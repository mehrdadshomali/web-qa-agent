#!/usr/bin/env python3
"""
web-qa-agent — Faz 1: salt-okunur gezme ve veri toplama.

Hedef: Docker ile çalışan bir Laravel 12 uygulaması (varsayılan http://localhost:8080).
Bu betik SADECE GET navigasyonu yapar. Hiçbir form göndermez, durum değiştiren
hiçbir işlem yapmaz, giriş yapmaz, dış servise istek atmaz (yapay zeka yok).

Bağımlılıklar: playwright, beautifulsoup4
"""

import argparse
import json
import os
import re
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urldefrag, urlparse, urlunparse

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError


# --- Ayarlar ---------------------------------------------------------------

# Durum değiştiren / auth / ödeme kalıpları — bu URL'ler crawl edilmez.
SKIP_PATTERNS = re.compile(
    r"("
    # --- İngilizce (durum-değiştiren / auth / ödeme) ---
    r"logout|signout|sign-out|delete|destroy|remove|edit|checkout|payment|"
    r"\bpay\b|cart|add-to-cart|purchase|ticket|subscribe|unsubscribe|"
    # --- Türkçe: sepet / ödeme / satın alma / fatura ---
    r"sepet|sepete-ekle|odeme|ödeme|fatura|satin|satın|"
    # --- Türkçe: abonelik — SADECE aksiyon biçimleri; içerik kategorisi
    #     'abonelik-eglence' yanlışlıkla atlanmasın diye bare 'abonelik' YOK ---
    r"abone-ol|abonelik-iptal|abonelikten|"
    # --- Türkçe: düzenleme / değiştirme / güncelleme ---
    r"duzenle|düzenle|degistir|değiştir|guncelle|güncelle|"
    # --- Türkçe: çıkış / oturum kapatma (KRİTİK: ajan kendi oturumunu kapatmasın) ---
    r"cikis|çıkış|oturumu-kapat|oturum-kapat|"
    # --- Türkçe: bilet ---
    r"bilet|"
    # --- Türkçe: silme — literal 'silme' + segment-sınırlı 'sil'
    #     (böylece 'nasil', 'nesil', 'temsil' gibi masum slug'lar ATLANMAZ) ---
    r"silme|(?<![a-z])sil(?![a-z])|"
    # --- Türkçe: kaldırma — segment-sınırlı ('kaldirim' korunur) ---
    r"(?<![a-z])kald[ıi]r(?![a-z])|"
    # --- Türkçe: topluluk/kulüpten ayrılma — segment-sınırlı ('ayrilik' korunur) ---
    r"(?<![a-z])ayr[ıi]l(?![a-z])"
    r")",
    re.IGNORECASE,
)

# Login'e redirect olduğunu anlamak için (auth duvarı).
AUTH_URL_PATTERNS = re.compile(r"(/login|/giris|/sign-in|/signin)", re.IGNORECASE)

# Sayfa yükleme stratejisi
NAV_TIMEOUT_MS = 15_000      # makul navigation timeout
ALPINE_WAIT_MS = 800         # Alpine.js init için sabit bekleme
INTER_PAGE_DELAY_S = 0.4     # sayfalar arası küçük gecikme

VITE_HMR_HOSTS = {"localhost:5173", "127.0.0.1:5173"}

# Tarayıcıyla "sayfa" olarak açılmayacak, indirilebilir dosya uzantıları.
DOWNLOAD_EXTENSIONS = (
    ".pdf", ".zip", ".rar", ".7z", ".gz", ".tar",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".csv", ".mp4", ".mp3", ".wav", ".mov", ".dmg", ".exe", ".apk",
)


# --- Yardımcılar -----------------------------------------------------------

def strip_query(url):
    """Query string ve fragment'i atarak URL'i path bazında normalize et.
    /etkinlikler?sort=a ve /etkinlikler?sort=b -> aynı normalize URL."""
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def is_downloadable(url):
    """Path bilinen bir indirme uzantısıyla bitiyor mu?"""
    path = urlparse(url).path.lower()
    return path.endswith(DOWNLOAD_EXTENSIONS)

def normalize_url(href, base_url):
    """Göreli linki mutlaklaştır, fragment'i at. Crawl edilemez şema ise None döner."""
    if not href:
        return None
    href = href.strip()
    if not href:
        return None
    lowered = href.lower()
    if lowered.startswith(("mailto:", "tel:", "javascript:", "data:")) or href.startswith("#"):
        return None
    absolute = urljoin(base_url, href)
    absolute, _ = urldefrag(absolute)
    scheme = urlparse(absolute).scheme
    if scheme not in ("http", "https"):
        return None
    return absolute


def same_host(url, base_host):
    return urlparse(url).netloc == base_host


def should_skip(url):
    """State değiştiren / auth / admin URL'lerini ATLA."""
    parsed = urlparse(url)
    path = parsed.path
    if re.search(r"/admin(/|$)", path, re.IGNORECASE):
        return True
    if SKIP_PATTERNS.search(path) or (parsed.query and SKIP_PATTERNS.search(parsed.query)):
        return True
    return False


def slugify_for_filename(url):
    parsed = urlparse(url)
    raw = (parsed.path or "/") + (("?" + parsed.query) if parsed.query else "")
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", raw).strip("_")
    if not slug:
        slug = "root"
    return slug[:120]


def extract_exception_info(html):
    """APP_DEBUG=true ile 500 veren Laravel/Ignition sayfasından istisna bilgisini topla."""
    info = {}
    try:
        soup = BeautifulSoup(html, "html.parser")
        if soup.title and soup.title.string:
            info["title"] = soup.title.string.strip()
        # Ignition sayfası exception sınıfını ve mesajını çeşitli yerlerde gösterir.
        text = soup.get_text("\n", strip=True)
        m = re.search(r"([A-Za-z0-9_\\]+(?:Exception|Error))\b", text)
        if m:
            info["exception_class"] = m.group(1)
        # Yaygın Laravel hata satırı kalıpları
        m2 = re.search(r"(SQLSTATE\[[^\]]+\][^\n]*)", text)
        if m2:
            info["message"] = m2.group(1)[:300]
    except Exception:
        pass
    return info or None


# --- Ana toplama mantığı ---------------------------------------------------

def check_target_status(request_ctx, url, status_cache):
    """Bir <a>/<img> hedefinin durumunu hafifçe kontrol et (HEAD, gerekirse GET)."""
    if url in status_cache:
        return status_cache[url]
    status = None
    try:
        resp = request_ctx.head(url, timeout=10_000, max_redirects=5)
        status = resp.status
        # Bazı sunucular HEAD'i desteklemez -> GET ile dene
        if status in (403, 405, 501):
            resp = request_ctx.get(url, timeout=10_000, max_redirects=5)
            status = resp.status
    except PlaywrightError:
        status = None  # ulaşılamadı / ağ hatası
    status_cache[url] = status
    return status


def visit_page(browser, request_ctx, url, base_host, screenshots_dir, status_cache,
               auth_state=None):
    """Tek bir sayfayı ziyaret et, salt-okunur veri topla. Bulunan iç linkleri döndür.
    auth_state verilirse (login sonrası cookie'ler) context authenticated olur."""
    result = {
        "url": url,
        "final_url": None,
        "status": None,
        "load_failed": False,
        "downloadable": None,         # {"status": int|None} -> indirilebilir dosya, hata değil
        "query_variants_seen": [],    # bu path'in görülen query varyantları (sadece ilki ziyaret edilir)
        "auth_required": False,
        "exception": None,
        "console_messages": [],
        "site_resources": [],         # host == base_host -> GERÇEK kırık kaynak
        "third_party_resources": [],  # dış hostlar (GA, GTM, fonts, fb, vb.)
        "vite_hmr_assets": [],        # localhost:5173 -> dev-server / HMR
        "broken_links": [],
        "broken_images": [],
        "forms": [],
        "screenshot": None,
    }
    discovered_internal = []

    # İndirilebilir dosyalar: tarayıcıyla "sayfa" olarak açma; sadece hafif
    # HEAD/GET ile HTTP durumunu al ve "downloadable" olarak kaydet (hata değil).
    if is_downloadable(url):
        result["downloadable"] = {"status": check_target_status(request_ctx, url, status_cache)}
        return result, discovered_internal

    context = browser.new_context(ignore_https_errors=True, storage_state=auth_state)
    page = context.new_page()
    page.set_default_navigation_timeout(NAV_TIMEOUT_MS)

    console_messages = []
    page.on("console", lambda msg: console_messages.append(
        {"type": msg.type, "text": msg.text}))
    page.on("pageerror", lambda exc: console_messages.append(
        {"type": "pageerror", "text": str(exc)}))

    # Başarısız ağ isteklerini host'a göre sınıflandır:
    #  - site_resource  : host == base_host -> GERÇEK kırık kaynak
    #  - vite_hmr        : localhost:5173    -> dev-server / HMR varlığı
    #  - third_party     : diğer tüm dış hostlar (GA, GTM, fonts, fb, vb.)
    # status=None (engellenmiş/başarısız dış beacon) da bu sınıflandırmaya girer.
    def classify_failed(entry):
        host = urlparse(entry["url"]).netloc
        if host in VITE_HMR_HOSTS:
            entry["category"] = "vite_hmr"
            result["vite_hmr_assets"].append(entry)
        elif host == base_host:
            entry["category"] = "site_resource"
            result["site_resources"].append(entry)
        else:
            entry["category"] = "third_party"
            result["third_party_resources"].append(entry)

    def on_response(resp):
        try:
            if resp.status >= 400:
                classify_failed({
                    "url": resp.url,
                    "status": resp.status,
                    "resource_type": resp.request.resource_type,
                })
        except Exception:
            pass

    def on_requestfailed(req):
        try:
            classify_failed({
                "url": req.url,
                "status": None,
                "failure": (req.failure or "request failed"),
                "resource_type": req.resource_type,
            })
        except Exception:
            pass

    page.on("response", on_response)
    page.on("requestfailed", on_requestfailed)

    try:
        response = page.goto(url, wait_until="load")
        if response is not None:
            result["status"] = response.status
        page.wait_for_timeout(ALPINE_WAIT_MS)  # Alpine.js init
    except PlaywrightTimeoutError:
        result["load_failed"] = True
        result["console_messages"] = console_messages
        context.close()
        return result, discovered_internal
    except PlaywrightError as e:
        result["console_messages"] = console_messages
        context.close()
        # Uzantısız ama indirme tetikleyen uçlar: "Download is starting" -> hata değil.
        if "Download is starting" in str(e):
            result["downloadable"] = {"status": check_target_status(request_ctx, url, status_cache)}
        else:
            result["load_failed"] = True
            result["error"] = str(e)
        return result, discovered_internal

    final_url = page.url
    result["final_url"] = final_url

    # Auth duvarı: login'e redirect olduysa kaydet ve atla.
    if AUTH_URL_PATTERNS.search(urlparse(final_url).path) and not AUTH_URL_PATTERNS.search(urlparse(url).path):
        result["auth_required"] = True

    html = page.content()

    # 500 + APP_DEBUG=true -> istisna bilgisi
    if result["status"] and result["status"] >= 500:
        result["exception"] = extract_exception_info(html)

    soup = BeautifulSoup(html, "html.parser")

    # Form envanteri (SADECE tespit, gönderme yok)
    for form in soup.find_all("form"):
        action = form.get("action") or final_url
        method = (form.get("method") or "GET").upper()
        required = []
        has_csrf = False
        for inp in form.find_all(["input", "select", "textarea"]):
            name = inp.get("name", "")
            if name in ("_token", "csrf_token") or inp.get("type") == "hidden" and name == "_token":
                has_csrf = True
            if inp.has_attr("required"):
                required.append(name or inp.get("type", "?"))
        result["forms"].append({
            "action": urljoin(final_url, action),
            "method": method,
            "has_csrf_token": has_csrf,
            "required_inputs": required,
        })

    # <a> hedefleri: iç linkleri crawl kuyruğuna, hepsinin durumunu kontrol et
    for a in soup.find_all("a", href=True):
        target = normalize_url(a["href"], final_url)
        if not target:
            continue
        if same_host(target, base_host):
            if not should_skip(target):
                discovered_internal.append(target)
        # Durum kontrolü (iç + dış), kırıkları raporla
        st = check_target_status(request_ctx, target, status_cache)
        if st is not None and st >= 400:
            result["broken_links"].append({"url": target, "status": st})

    # <img> hedefleri
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src")
        target = normalize_url(src, final_url)
        if not target:
            continue
        st = check_target_status(request_ctx, target, status_cache)
        if st is not None and st >= 400:
            result["broken_images"].append({"url": target, "status": st})

    # Ekran görüntüsü
    shot_path = screenshots_dir / f"{slugify_for_filename(url)}.png"
    try:
        page.screenshot(path=str(shot_path), full_page=True)
        result["screenshot"] = str(shot_path)
    except PlaywrightError:
        pass

    result["console_messages"] = console_messages
    context.close()
    return result, discovered_internal


# --- Login (salt-okunur istisna) -------------------------------------------

def do_login(browser, base_url, email, password):
    """SALT-OKUNUR İSTİSNA: yalnızca /login formunu doldurup gönderir.

    Başka hiçbir form doldurulmaz/gönderilmez. CSRF token elle scrape edilmez —
    form @csrf içerir ve Playwright submit edince tarayıcı token/cookie'yi taşır.
    (basari, storage_state, final_url) döner.
    """
    login_url = base_url + "/login"
    context = browser.new_context(ignore_https_errors=True)
    page = context.new_page()
    page.set_default_navigation_timeout(NAV_TIMEOUT_MS)
    try:
        page.goto(login_url, wait_until="load")
        page.wait_for_timeout(ALPINE_WAIT_MS)  # Alpine init
        page.fill('input[name="email"]', email)
        page.fill('input[name="password"]', password)
        # Formu Playwright submit etsin (tarayıcı CSRF token/cookie'yi kendi taşır).
        try:
            with page.expect_navigation(wait_until="load", timeout=NAV_TIMEOUT_MS):
                page.click('button[type="submit"]')
        except PlaywrightTimeoutError:
            pass  # yönlendirme olmayabilir; başarıyı aşağıda URL/logout ile belirleriz
        page.wait_for_timeout(ALPINE_WAIT_MS)
    except PlaywrightError as e:
        context.close()
        return False, None, str(e)

    final_url = page.url
    on_login_page = AUTH_URL_PATTERNS.search(urlparse(final_url).path) is not None
    has_logout = page.query_selector(
        'a[href*="logout"], form[action*="logout"], a[href*="signout"]') is not None
    success = (not on_login_page) or has_logout
    state = context.storage_state()
    context.close()
    return success, state, final_url


# --- Çalıştırıcı -----------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="web-qa-agent Faz 1 — salt-okunur gezme ve veri toplama")
    parser.add_argument("--url", default="http://localhost:8080",
                        help="Base URL (varsayılan: http://localhost:8080)")
    parser.add_argument("--max-pages", type=int, default=50,
                        help="Gezilecek maksimum sayfa (varsayılan: 50)")
    parser.add_argument("--login", action="store_true",
                        help="Crawl'dan önce QA test hesabıyla login ol (auth arkası "
                             "youth sayfaları da taranır). .env: QA_LOGIN_EMAIL/PASSWORD")
    args = parser.parse_args()

    base_url = args.url.rstrip("/")
    base_host = urlparse(base_url).netloc

    reports_dir = Path(__file__).parent / "reports"
    screenshots_dir = reports_dir / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        request_ctx = p.request.new_context(ignore_https_errors=True)

        # İLK İŞ: base URL erişilebilir mi?
        try:
            probe = request_ctx.get(base_url, timeout=10_000)
            reachable = probe.status < 600  # herhangi bir HTTP yanıtı = ayakta
        except PlaywrightError:
            reachable = False

        if not reachable:
            print("\n[HATA] Base URL'e erişilemiyor: %s" % base_url)
            print("       Önce badinext'te 'docker compose up' ile uygulamayı başlat.\n")
            request_ctx.dispose()
            sys.exit(1)

        browser = p.chromium.launch(headless=True)

        # Opsiyonel login (salt-okunur istisna: sadece /login formu).
        auth_state = None
        login_status = "kapalı"
        if args.login:
            load_dotenv(Path(__file__).parent / ".env")
            email = os.environ.get("QA_LOGIN_EMAIL", "test-youth@qa.local").strip()
            password = os.environ.get("QA_LOGIN_PASSWORD", "")
            if not password:
                print("\n[HATA] --login verildi ama QA_LOGIN_PASSWORD .env'de yok.")
                print("       web-qa-agent/.env dosyasına QA_LOGIN_PASSWORD=... ekleyin.\n")
                browser.close()
                request_ctx.dispose()
                sys.exit(1)
            print(f"[login] Giriş deneniyor: {email} ...")
            ok, auth_state, final_url = do_login(browser, base_url, email, password)
            if not ok:
                login_status = "başarısız"
                print("\n[HATA] Login başarısız — email/şifre .env'de doğru mu, hesap active mi?")
                print(f"       Giriş sonrası URL: {final_url}\n")
                browser.close()
                request_ctx.dispose()
                sys.exit(1)
            login_status = "başarılı"
            print(f"[login] Başarılı. Oturum korunuyor. (URL: {final_url})")
            # HEAD/link durum kontrolleri de authenticated olsun:
            request_ctx.dispose()
            request_ctx = p.request.new_context(ignore_https_errors=True,
                                                storage_state=auth_state)

        # Query string'i atılmış (path-bazlı) normalize URL'lerle benzersizleştir:
        # /etkinlikler?sort=a ve ?sort=b aynı sayfa sayılır, sadece ilki ziyaret edilir.
        start = strip_query(base_url)
        visited = set()
        queued = {start}                 # kuyrukta/işlenmiş normalize URL'ler (O(1) kontrol)
        queue = deque([start])
        query_variants = {}              # normalize URL -> görülen query varyantları seti
        findings = []
        status_cache = {}

        while queue and len(visited) < args.max_pages:
            url = queue.popleft()
            if url in visited:
                continue
            if should_skip(url):
                continue
            visited.add(url)

            print(f"[{len(visited):>3}/{args.max_pages}] Ziyaret: {url}")
            result, discovered = visit_page(
                browser, request_ctx, url, base_host, screenshots_dir, status_cache,
                auth_state)
            # Bu path için görülen query varyantlarını kayda ekle (bilgi kaybetme).
            result["query_variants_seen"] = sorted(query_variants.get(url, set()))
            findings.append(result)

            if not result["auth_required"]:
                for link in discovered:
                    norm = strip_query(link)
                    q = urlparse(link).query
                    if q:
                        query_variants.setdefault(norm, set()).add(q)
                    if norm not in queued:
                        queued.add(norm)
                        queue.append(norm)

            time.sleep(INTER_PAGE_DELAY_S)

        browser.close()
        request_ctx.dispose()

    # Kırık link/görselleri benzersiz HEDEF bazında topla (rapor şişmesin).
    # Aynı kırık URL (ör. menüdeki /kariyer) birçok sayfada tekrar geçtiği için
    # sayfa-bazlı kayıtlar şişik görünür; burada benzersiz hedefe indirgenir.
    # Sayfa-bazlı kayıtlar (f["broken_links"]/["broken_images"]) AYNEN kalır.
    broken_targets = {}
    for f in findings:
        for kind, items in (("link", f["broken_links"]), ("image", f["broken_images"])):
            for b in items:
                t = broken_targets.setdefault(b["url"], {
                    "url": b["url"],
                    "status": b["status"],
                    "kind": kind,
                    "reference_count": 0,   # toplam kaç kez referans verildi
                    "found_on_pages": [],   # hangi sayfalarda göründü
                })
                t["reference_count"] += 1
                if f["url"] not in t["found_on_pages"]:
                    t["found_on_pages"].append(f["url"])
    broken_targets_list = list(broken_targets.values())

    # İndirilebilir dosyaları ayrı kategoride topla (yüklenemeyen sayfa DEĞİL).
    downloadable_files = [
        {"url": f["url"], "status": f["downloadable"].get("status")}
        for f in findings if f["downloadable"]
    ]

    # Çıktıyı yaz
    limit_reached = len(visited) >= args.max_pages and len(queue) > 0
    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": base_url,
        "login": login_status,
        "pages_visited": len(findings),
        "max_pages_limit_reached": limit_reached,
        "broken_targets": broken_targets_list,
        "downloadable_files": downloadable_files,
        "pages": findings,
    }
    findings_path = reports_dir / "findings.json"
    findings_path.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")

    # Özet
    non_2xx = sum(1 for f in findings
                  if f["status"] is not None and not (200 <= f["status"] < 300))
    load_failed = sum(1 for f in findings if f["load_failed"])
    console_errors = sum(
        1 for f in findings for m in f["console_messages"]
        if m["type"] in ("error", "pageerror"))
    broken_refs = sum(len(f["broken_links"]) + len(f["broken_images"]) for f in findings)
    broken_unique = len(broken_targets_list)
    site_resources = sum(len(f["site_resources"]) for f in findings)
    third_party = sum(len(f["third_party_resources"]) for f in findings)
    vite_hmr = sum(len(f["vite_hmr_assets"]) for f in findings)
    auth_skipped = sum(1 for f in findings if f["auth_required"])
    total_forms = sum(len(f["forms"]) for f in findings)

    limit_note = "SINIRA ULAŞILDI (daha fazla sayfa var)" if limit_reached else "tüm yüzey kapsandı"
    print("\n" + "=" * 60)
    print("ÖZET")
    print("=" * 60)
    print(f"  Login durumu             : {login_status}")
    print(f"  Gezilen benzersiz path   : {len(findings)}  -> {limit_note}")
    print(f"  2xx olmayan durum        : {non_2xx}")
    print(f"  Yüklenemeyen sayfa       : {load_failed}")
    print(f"  İndirilebilir dosya      : {len(downloadable_files)}")
    print(f"  Konsol hatası (toplam)   : {console_errors}")
    print(f"  Kırık hedef              : {broken_unique} benzersiz ({broken_refs} referans)")
    print(f"  Kırık kaynak (site)      : {site_resources}")
    print(f"  Başarısız istek (dış/3P) : {third_party}")
    print(f"  Vite/HMR varlık uyarısı  : {vite_hmr}")
    print(f"  Auth nedeniyle atlanan   : {auth_skipped}")
    print(f"  Bulunan form sayısı      : {total_forms}")
    print("=" * 60)
    print("  Not: aynı sayfanın 500 hatası birden çok kategoride sayılabilir")
    print("       (kırık link + 2xx-olmayan sayfa + konsol hatası).")
    print("=" * 60)
    print(f"\nRapor: {findings_path}")
    print(f"Ekran görüntüleri: {screenshots_dir}\n")


if __name__ == "__main__":
    main()
