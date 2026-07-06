#!/usr/bin/env python3
"""
web-qa-agent — Faz 4: haftalık otomatik çalışma (launchd tarafından tetiklenir).

Tek seferlik çalışır: run_qa() ile tam tarama (login, max 200) yapar, sonucu
telegram_bot.send_report_to_telegram() ile Telegram'a gönderir, sonra çıkar.
(Bot'un komut dinlemesine gerek yoktur; bu ayrı, kısa ömürlü bir süreçtir.)

Mevcut kodlara DOKUNMAZ — yalnızca run_qa ve telegram_bot'u import eder.

Tarama başarısızsa (ör. site kapalı / docker down) run_qa zaten analize geçmez,
yani API çağrısı YAPILMAZ; bu durumda Telegram'a net bir hata gönderilir.

launchd kurulumu + elle test için: bkz. README.md → "Faz 4: Haftalık otomatik çalışma".
"""

import asyncio
import sys
from datetime import datetime
from pathlib import Path

from telegram import Bot

from run_qa import run_qa
from telegram_bot import TOKEN, CHAT_ID, QA_URL, send_report_to_telegram

BASE = Path(__file__).parent
REPORT = BASE / "reports" / "report.md"
LOG_DIR = BASE / "logs"
RUN_LOG = LOG_DIR / "weekly.log"

MAX_PAGES = 200
LOGIN = True


def _log(line):
    """Çalışma geçmişini logs/weekly.log'a (tarih + sonuç) ekle ve stdout'a bas."""
    LOG_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    with open(RUN_LOG, "a", encoding="utf-8") as f:
        f.write(f"{stamp} | {line}\n")
    print(f"{stamp} | {line}", flush=True)


async def _send(result):
    """Telegram'a haftalık başlık + özet + (başarılıysa) report.md gönder."""
    bot = Bot(token=TOKEN)
    async with bot:  # PTB Bot'u başlat/kapat
        await bot.send_message(chat_id=CHAT_ID, text="🗓 Haftalık QA taraması")
        await send_report_to_telegram(bot, CHAT_ID, result, REPORT)


def main():
    if not TOKEN or not CHAT_ID:
        _log("HATA: TELEGRAM_BOT_TOKEN/CHAT_ID .env'de yok — rapor gönderilemez.")
        sys.exit(1)

    _log(f"Haftalık tarama başladı (url={QA_URL}, max_pages={MAX_PAGES}, login={LOGIN})")

    try:
        result = run_qa(url=QA_URL, max_pages=MAX_PAGES, login=LOGIN)
    except Exception as e:
        _log(f"HATA: tarama sırasında beklenmeyen istisna: {e}")
        try:
            asyncio.run(_send({"ok": False, "stage": "scan", "pages": None,
                               "login": None, "critical": None,
                               "report_path": None, "cost_usd": None}))
        except Exception as e2:
            _log(f"HATA: Telegram bildirimi de başarısız: {e2}")
        sys.exit(1)

    if result.get("ok"):
        _log(f"Tarama OK: pages={result['pages']} critical={result['critical']} "
             f"cost={result.get('cost_usd')}")
    else:
        _log(f"Tarama BAŞARISIZ: stage={result.get('stage')} "
             "(scan aşamasıysa site erişilemedi; API çağrısı yapılmadı)")

    try:
        asyncio.run(_send(result))
        _log("Telegram raporu gönderildi.")
    except Exception as e:
        _log(f"HATA: Telegram gönderimi başarısız: {e}")
        sys.exit(1)

    sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
