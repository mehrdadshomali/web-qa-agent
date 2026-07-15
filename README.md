# web-qa-agent

![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)
![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)
![Tested with Playwright](https://img.shields.io/badge/E2E-Playwright-2EAD33.svg)
![AI](https://img.shields.io/badge/AI-Anthropic%20Claude-D97757.svg)

**AI-powered QA + E2E test agent** — connects to any web application through a
browser. It has two complementary capabilities:

1. **Read-only QA crawler + AI report generator** — walks the guest and (optionally)
   logged-in surface; collects technical, accessibility, and performance data, and
   produces a prioritized quality report with Anthropic Claude. Optionally delivers
   the result to Telegram and runs weekly on a schedule.
2. **End-to-end (E2E) flow tests** — actually *drives* critical user journeys with
   Playwright (clicks, fills forms, checks out). This part takes **active** actions
   but sits behind a strict **safe-environment gate**: it runs only in a
   **local/isolated** environment where mail is written to the log and payments go to
   a fake provider (see below).

**Read-only crawler (capability 1):** `GET` navigation only. No form is ever submitted
(they are only inventoried); state-changing endpoints (logout, delete, checkout,
cart/payment, `/admin`, etc. — both English and Turkish patterns) are skipped. The
sole exception is filling and submitting the `/login` form when explicitly requested
(to crawl behind auth).

**E2E flows (capability 2):** real state-changing actions (see
[E2E flow tests](#e2e-flow-tests)). Each run reads the target's `.env` and verifies
`MAIL_MAILER=log` and that the payment provider is `fake`/empty; otherwise it refuses
to run — so no action is ever taken in an environment that has accidentally switched
to real mail/payment mode.

Designed for server-side-rendered applications (e.g. Blade + Alpine.js style stacks;
heavy animation libraries — Three.js/GSAP — have been taken into account). The
read-only crawler never touches the target's source code or database; the E2E flows
only touch the target's *local* test database for repeatability (teardown), in a
strict, narrowly-scoped way.

---

## Architecture / flow

```
# Read-only QA crawler + AI report
runner.py     →  reports/findings.json      (read-only scan: technical + a11y + perf)
analyze.py    →  reports/report.md          (distills findings and sends to Claude)
run_qa.py     →  chains the two above in a single command
telegram_bot.py →  /tara, /test commands via a bot; delivers the report to Telegram
weekly_run.py + launchd  →  weekly automated run + Telegram delivery

# Active E2E flow tests (behind the safe-environment gate)
flow_tests/ticket_cart_flow.py  →  cart / payment / registration+mail E2E flows (headed)
```

The read-only layers are thin and each calls the one below it; they don't touch the
lower components. The E2E module is separate: it **reuses** the crawler's `do_login`
and listener logic but does not modify it.

---

## Requirements

- Python 3.11+
- An Anthropic API key (for the AI report) — https://console.anthropic.com
- (Optional) A Telegram bot — @BotFather
- A running target web application to test (default `http://localhost:8080`)

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# Configuration: create your own .env from the example and fill in the values
cp .env.example .env
# Open .env and set ANTHROPIC_API_KEY, etc.
```

`.env` is **not** committed (`.gitignore`). Never commit your keys.

### `.env` variables

| Variable | Purpose | Required? |
|---|---|---|
| `ANTHROPIC_API_KEY` | AI report (`analyze.py`) | Yes, for the AI report |
| `QA_LOGIN_EMAIL` / `QA_LOGIN_PASSWORD` | Crawl behind auth via `--login` | For login crawl |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Telegram delivery | For Telegram/weekly |
| `TARGET_REPO_PATH` | Target app root (optional; enables code-backed diagnosis and E2E safe-env gate) | No |

> For login, use a **disposable test account** in the target app (not a real user).
> This tool is only meant to read-only crawl the surface that the guest + that test
> role can *see*.

---

## Usage

### 1) Crawl — `runner.py`

```bash
python runner.py                                   # default: --url http://localhost:8080 --max-pages 50
python runner.py --url http://localhost:8080 --max-pages 200
python runner.py --login --max-pages 200           # also crawl behind auth (using .env credentials)
```

If the target is unreachable, it prints a clear message and exits cleanly. For each
unique page it collects:

- **HTTP status code** (non-2xx flagged); on 500s, the framework exception type/message
  (e.g. Laravel, assuming `APP_DEBUG=true`).
- **Console messages** (error/warning prioritized).
- **Failed network requests**, categorized by host: `site_resource` (a real broken
  site resource), `third_party` (external: analytics, etc.), `vite_hmr` (dev-server).
- **Broken links/images** (`<a>`/`<img>` targets via lightweight HEAD/GET);
  deduplicated in the global `broken_targets` summary.
- **Form inventory** (detection, **no submission**): count, action, method, CSRF
  field, required inputs.
- **Accessibility**: WCAG 2.0/2.1 A+AA violations via a vendored axe-core.
- **Performance** (lightweight, no Lighthouse): load time, DOMContentLoaded,
  transferred bytes, request count.
- Full-page **screenshot**.

**Efficiency:** URLs are deduplicated by path (query variants count as one page);
downloadable files (PDF, etc.) are kept in a separate category.

### 2) AI report — `analyze.py`

```bash
python analyze.py
```

**Distills** `findings.json` (raw node lists/screenshots are not sent; only a compact
summary), sends it to `claude-sonnet-4-6`, and writes a prioritized (Critical/Medium/Low)
report to `reports/report.md`. The report separates **measured findings** (certain) from
**root-cause inferences** (based on external observation, to be verified). If the API
fails, it produces a local report without AI; token usage + rough cost are printed on
every run.

### 3) Single-command chain — `run_qa.py`

```bash
python run_qa.py                    # full crawl (login, 200 pages) + AI report
python run_qa.py --max-pages 50
python run_qa.py --no-login         # guest only
```

If the crawl fails (target down / login failed), it does **not** proceed to the AI
step — no money spent calling the API with half the data. Can be invoked both from the
CLI and programmatically (`run_qa()`).

### 4) Telegram bot — `telegram_bot.py`

```bash
python telegram_bot.py
```

Responds only to the `TELEGRAM_CHAT_ID` in `.env` (ignores everyone else). Commands:
`/start` (help), `/tara` (full crawl; `/tara 50` to override page count), `/test`
(quick 5 pages). When done it sends a summary + the `report.md` file. To find your
`chat_id`: send the bot a message, then run `python get_chat_id.py`.

### 5) Weekly automated run (macOS launchd) — `weekly_run.py`

`weekly_run.py` is a one-shot script that runs the full crawl via `run_qa()` and
delivers the result to Telegram. The sample `launchd` plist under `deploy/` triggers
it once a week.

```bash
# Copy the sample plist and edit the PATHS inside it for your setup
cp deploy/com.example.qa-weekly.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.example.qa-weekly.plist
launchctl list | grep qa-weekly

# Manual test (run once without waiting for the schedule):
launchctl start com.example.qa-weekly
#   or directly (without launchd):
venv/bin/python weekly_run.py

# Uninstall:
launchctl unload -w ~/Library/LaunchAgents/com.example.qa-weekly.plist
```

Run history goes to `logs/weekly.log`; launchd output goes to `logs/launchd.*.log`.

**⚠️ Mac sleep / powered-off behavior:**
- **Awake:** the job runs on time.
- **Asleep:** launchd runs the missed job **once** when the Mac wakes (default; no
  extra config needed).
- **Fully powered off:** the run is **not guaranteed** — the Mac must be on (awake or
  asleep) at that time. Optionally wake the Mac before the job with:
  `sudo pmset repeat wakeorpoweron M 08:55:00`. If missing a run is critical, an
  always-on server/CI (cron) is a better fit.

---

## E2E flow tests

`flow_tests/ticket_cart_flow.py` **actually drives** critical user journeys with
Playwright (headed/visible by default). Unlike the read-only crawler, these flows
**change state** (clicks, forms, purchases); that's why every run starts with a strict
safe-environment gate.

### Safety gate (before every run)

The flow reads the target app's `src/.env` and proceeds **only** if:

- `MAIL_MAILER=log` → mail never goes out, it is only written to the target's log.
- The payment provider is `fake` or empty → no real payment provider (real money) is hit.

If the conditions aren't met, or `src/.env` can't be found, the run is **refused** (no
action taken). The target's path is resolved from `TARGET_REPO_PATH` in
`web-qa-agent/.env`, or provided via `--target-env`.

### Prerequisites

- A running **local/isolated** target app (up via `docker compose`).
- Seeded **disposable test data** in the target: a test user (complete profile +
  verified email) and a purchasable test record. (This tool adds no files to the
  target's code; you prepare test data with the target's own seeding mechanism.)

### The three flows

```bash
# 1) Cart flow (default): add item → increase quantity in cart → verify
python flow_tests/ticket_cart_flow.py

# 2) Payment flow: continues the cart flow → billing → confirm → pay via fake gateway → verify
python flow_tests/ticket_cart_flow.py --pay

# 3) Registration + verification mail: register a new (throwaway) user → is the mail in the log?
python flow_tests/ticket_cart_flow.py --register-mail

# Run gates only (safety + login + profile), don't enter the flow:
python flow_tests/ticket_cart_flow.py --gates-only

# Headless instead of visible (CI):
python flow_tests/ticket_cart_flow.py --no-headed
```

| Flow | What it does | Definition of "success" |
|---|---|---|
| **Cart** (default) | Login → **dynamically** selects a suitable item → adds to cart → increases quantity in the cart | Each step verified in the DOM; no console/pageerror/4xx/5xx |
| **Payment** (`--pay`) | Continues the cart flow → billing details → confirm → completes with the **fake** payment provider | Redirects to the success page + visible success message; no real money |
| **Register+mail** (`--register-mail`) | Guest → registers a new user → verification mail is triggered | Registration completes **and** the verification mail is written to the target's log (without going out) |

Selectors are derived from the target's **existing structural** elements (form
`action`/`name`, button text, stable `id`/attribute); no test-specific markers
(`data-testid`) are added to the target's templates.

### Repeatability (teardown)

Each flow starts clean and leaves no persistent test residue:

- **Cart/payment:** at the start of the flow the test user's cart is cleared via the
  app's own "remove" flow; the payment flow additionally deletes paid orders belonging
  to the test user's **test record only**, via a **double-condition** (user + record id)
  scoped cleanup (related rows go via FK cascade).
- **Register+mail:** each run generates a unique `qa-mailtest-{time}@qa.local` email; at
  the end that user is deleted with a **triple guard** (prefix + domain + exact match) —
  making it impossible to touch a real or different user.

Teardown runs against the target's local test database via `docker compose exec`; if it
fails (e.g. the container is down), the flow does not blindly continue — it stops with a
clear message.

---

## Outputs

- `reports/findings.json` — per-page raw findings + global summaries.
- `reports/report.md` — AI-generated prioritized report.
- `reports/screenshots/` — per-page screenshots.
- `logs/weekly.log` — weekly run history.

> `reports/` and `logs/` are **not** committed — scan findings (which may be
> target-specific) never enter the repo.

---

## Privacy & security

- All secrets live in `.env`; `.env` is gitignored and the API key/token is never logged.
- Scan findings (`reports/`) and logs (`logs/`) are not version-controlled.
- The **read-only crawler** takes no state-changing action on the target (only `GET` +
  `/login` if requested).
- The **E2E flows take active action**, but run only when the safe-environment gate
  passes (mail=log, payment=fake); use them only against a local/isolated target. Mail
  never goes out, payments are fake, and teardown scope is limited to test data.

## License

MIT — see [LICENSE](LICENSE).
