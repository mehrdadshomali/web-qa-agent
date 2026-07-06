#!/usr/bin/env python3
"""
Yardımcı: Telegram chat_id'yi bul.

Botu @BotFather'dan oluşturup TELEGRAM_BOT_TOKEN'i .env'e koyduktan sonra:
  1) Telegram'da botunuza herhangi bir mesaj yazın (ör. /start).
  2) Bu scripti çalıştırın: python get_chat_id.py
  3) Basılan chat_id'yi .env'e TELEGRAM_CHAT_ID olarak koyun.

Telegram getUpdates uç noktasını stdlib (urllib) ile çağırır — ekstra bağımlılık
yok. Token loglanmaz.
"""

import json
import os
import sys
import urllib.request
from pathlib import Path

from dotenv import load_dotenv


def main():
    load_dotenv(Path(__file__).parent / ".env")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("[HATA] TELEGRAM_BOT_TOKEN .env'de yok. @BotFather'dan alıp .env'e ekleyin.")
        sys.exit(1)

    url = f"https://api.telegram.org/bot{token}/getUpdates"  # URL basılmaz (token içerir)
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.load(resp)
    except Exception as e:
        print(f"[HATA] getUpdates çağrısı başarısız: {e}")
        sys.exit(1)

    if not data.get("ok"):
        print("[HATA] Telegram API 'ok' dönmedi. Token yanlış olabilir.")
        sys.exit(1)

    updates = data.get("result", [])
    if not updates:
        print("Henüz güncelleme yok. Botunuza Telegram'dan bir mesaj (/start) gönderip")
        print("bu scripti tekrar çalıştırın.")
        return

    seen = {}
    for u in updates:
        msg = u.get("message") or u.get("edited_message") or u.get("channel_post") or {}
        chat = msg.get("chat") or {}
        cid = chat.get("id")
        if cid is not None:
            seen[cid] = chat.get("username") or chat.get("title") or chat.get("first_name") or ""

    if not seen:
        print("Güncelleme var ama chat bilgisi çıkarılamadı.")
        return

    print("Bulunan chat_id'ler:")
    for cid, name in seen.items():
        print(f"  chat_id={cid}   ({name})")
    print("\nBunlardan sizin sohbetinize ait olanı .env'e TELEGRAM_CHAT_ID olarak koyun.")


if __name__ == "__main__":
    main()
