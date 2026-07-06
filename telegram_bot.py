#!/usr/bin/env python3
"""
web-qa-agent — Faz 4: Telegram botu.

run_qa.py'nin run_qa() fonksiyonunu çağırıp sonucu Telegram'a gönderir.
run_qa.py / runner.py / analyze.py'ye DOKUNMAZ; yalnızca run_qa'yı import eder.

Güvenlik: bot yalnızca .env'deki TELEGRAM_CHAT_ID'den gelen mesajlara cevap verir.
Başkası yazarsa sessizce yok sayılır (bu bot şirket sitesini tarar, herkese açık değil).

Komutlar:
  /start — kısa yardım
  /tara  — tam tarama (200 sayfa, login) + AI raporu. '/tara 50' ile sayfa override.
  /test  — hızlı deneme (5 sayfa, login).

Bağımlılık: python-telegram-bot (async, v20+). Token ASLA loglanmaz.
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from telegram.ext import Application, CommandHandler

from run_qa import run_qa

BASE = Path(__file__).parent
REPORT = BASE / "reports" / "report.md"

load_dotenv(BASE / ".env")
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
QA_URL = os.environ.get("QA_URL", "http://localhost:8080").strip()

# İzlenebilirlik: olayları loglar. Token ASLA loglanmaz.
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)     # PTB'nin HTTP gürültüsünü kıs
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("qa-bot")

# Aynı anda tek tarama yeter.
_scan_lock = asyncio.Lock()


def _authorized(update):
    """Yalnızca .env'deki TELEGRAM_CHAT_ID yetkili."""
    return bool(update.effective_chat) and str(update.effective_chat.id) == CHAT_ID


def _summary_text(result):
    """run_qa sonucu dict'inden kısa özet metni üret."""
    if not result.get("ok"):
        stage = {"scan": "tarama", "analyze": "AI analizi"}.get(result.get("stage"), "bilinmeyen")
        return (f"❌ QA zinciri başarısız — {stage} adımı.\n"
                "Site kapalı, login başarısız ya da API/anahtar hatası olabilir. "
                "Sunucu loglarına bakın.")
    cost = ("$%.4f" % result["cost_usd"]) if result.get("cost_usd") is not None else "bilinmiyor"
    return ("✅ QA taraması tamamlandı\n"
            f"• Taranan sayfa: {result.get('pages')} (login: {result.get('login')})\n"
            f"• Kritik bulgu: {result.get('critical')}\n"
            f"• API maliyeti: {cost}")


async def send_report_to_telegram(bot, chat_id, result, report_path):
    """Özet mesajını ve (başarılıysa) report.md dosyasını gönder.

    Ayrı fonksiyon: hem komut handler'ı hem ileride haftalık scheduler bunu kullanır.
    """
    await bot.send_message(chat_id=chat_id, text=_summary_text(result))
    if result.get("ok") and Path(report_path).exists():
        with open(report_path, "rb") as f:
            await bot.send_document(chat_id=chat_id, document=f, filename="qa-report.md")


async def _run_scan(update, context, max_pages, login):
    chat = getattr(update.effective_chat, "id", None)
    if not _authorized(update):
        log.warning("Yetkisiz istek yok sayıldı (chat_id=%s)", chat)
        return  # yetkisiz: sessizce yok say
    if _scan_lock.locked():
        log.info("Meşgul: eşzamanlı tarama isteği reddedildi (chat_id=%s)", chat)
        await update.message.reply_text("Zaten bir tarama sürüyor. Bitmesini bekleyin.")
        return
    async with _scan_lock:
        log.info("Tarama isteği: max_pages=%s login=%s (chat_id=%s)", max_pages, login, chat)
        await update.message.reply_text(
            f"Tarama başladı (en fazla {max_pages} sayfa, login "
            f"{'açık' if login else 'kapalı'}). Birkaç dakika sürebilir...")
        loop = asyncio.get_running_loop()
        try:
            # run_qa bloklar (subprocess) -> event loop'u tıkamamak için thread'de çalıştır.
            result = await loop.run_in_executor(None, run_qa, QA_URL, max_pages, login)
        except Exception as e:  # beklenmeyen çökme -> kullanıcıya bildir
            log.exception("Tarama sırasında beklenmeyen hata")
            await update.message.reply_text(f"❌ Beklenmeyen hata: {e}")
            return
        log.info("Tarama bitti: ok=%s pages=%s critical=%s cost=%s",
                 result.get("ok"), result.get("pages"), result.get("critical"),
                 result.get("cost_usd"))
        await send_report_to_telegram(context.bot, update.effective_chat.id, result, REPORT)


async def cmd_start(update, context):
    if not _authorized(update):
        log.warning("Yetkisiz /start yok sayıldı (chat_id=%s)",
                    getattr(update.effective_chat, "id", None))
        return
    log.info("/start (chat_id=%s)", update.effective_chat.id)
    await update.message.reply_text(
        "web-qa-agent botu 🤖\n\n"
        "/tara — tam tarama (200 sayfa, login) + AI raporu\n"
        "/tara 50 — 50 sayfa ile tarama\n"
        "/test — hızlı deneme (5 sayfa, login)\n\n"
        "Bitince özet + report.md dosyası gönderilir.")


async def cmd_tara(update, context):
    max_pages = 200
    if context.args and context.args[0].isdigit():
        max_pages = int(context.args[0])
    await _run_scan(update, context, max_pages=max_pages, login=True)


async def cmd_test(update, context):
    await _run_scan(update, context, max_pages=5, login=True)


def main():
    if not TOKEN or not CHAT_ID:
        print("[HATA] TELEGRAM_BOT_TOKEN veya TELEGRAM_CHAT_ID .env'de yok.")
        print("       web-qa-agent/.env dosyasına ekleyin (.env.example'a bakın).")
        print("       chat_id'yi öğrenmek için: python get_chat_id.py")
        sys.exit(1)

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("tara", cmd_tara))
    app.add_handler(CommandHandler("test", cmd_test))
    log.info("Bot çalışıyor. Yalnızca yetkili chat_id=%s yanıtlanır. Polling başlıyor...",
             CHAT_ID)
    app.run_polling()


if __name__ == "__main__":
    main()
