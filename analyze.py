#!/usr/bin/env python3
"""
web-qa-agent — Faz 2: beyin katmanı.

reports/findings.json'daki Faz 1 bulgularını DAMITIP Anthropic API'sine gönderir
ve önceliklendirilmiş, açıklamalı bir Markdown raporu (reports/report.md) üretir.

- Ham veri / ekran görüntüsü / sayfa-bazlı tekrar GÖNDERİLMEZ; sadece kompakt özet.
- API başarısız olursa yapay zeka OLMADAN yerel bir rapor üretilir (her zaman çıktı).
- ANTHROPIC_API_KEY .env'den yüklenir ve ASLA loglanmaz.

Bağımlılıklar: anthropic, python-dotenv
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
import os

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 4096
# Fiyatlandırma (USD / 1M token) — maliyet tahmini için.
PRICE_INPUT_PER_M = 3.00
PRICE_OUTPUT_PER_M = 15.00

REPORTS_DIR = Path(__file__).parent / "reports"
FINDINGS_PATH = REPORTS_DIR / "findings.json"
REPORT_PATH = REPORTS_DIR / "report.md"

# Gerçek konsol hatalarını üçüncü-taraf/kaynak gürültüsünden ayırmak için.
CONSOLE_NOISE = (
    "google-analytics", "googletagmanager", "doubleclick", "facebook",
    "gtag", "g/collect", "Failed to load resource",
)


def load_api_key():
    """ANTHROPIC_API_KEY'i .env'den yükle. Yoksa/placeholder ise None döner."""
    load_dotenv(Path(__file__).parent / ".env")
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key or key in ("BURAYA_ANAHTAR", "sk-ant-...") or not key.startswith("sk-ant-"):
        return None
    return key


def distill(findings):
    """findings.json'u API'ye gönderilecek kompakt bir özete indirger (ham veri yok)."""
    base_host = urlparse(findings["base_url"]).netloc
    pages = findings["pages"]
    exc_by_url = {p["url"]: p.get("exception") for p in pages if p.get("exception")}

    # Benzersiz kırık hedefler (500'lerde istisna mesajıyla)
    broken_targets = []
    for t in findings.get("broken_targets", []):
        entry = {
            "url": t["url"],
            "status": t["status"],
            "internal": urlparse(t["url"]).netloc == base_host,
            "reference_count": t["reference_count"],
        }
        exc = exc_by_url.get(t["url"])
        if t["status"] and t["status"] >= 500 and exc:
            entry["exception_class"] = exc.get("exception_class")
            entry["exception_message"] = exc.get("message")
        broken_targets.append(entry)

    # Gerçek site kırık kaynakları (benzersiz)
    site_resources = {}
    for p in pages:
        for e in p.get("site_resources", []):
            key = (e["url"], e.get("status"))
            site_resources.setdefault(key, {"url": e["url"], "status": e.get("status"),
                                            "resource_type": e.get("resource_type")})

    # Dış bağımlılık gözlemleri: gerçek 4xx/5xx dönen üçüncü-taraf kaynaklar
    # (status=None olan GA beacon'ları gürültü; bunları gözlem olarak dahil etmiyoruz).
    external_observations = {}
    for p in pages:
        for e in p.get("third_party_resources", []):
            st = e.get("status")
            if st is not None and st >= 400:
                external_observations.setdefault(e["url"], {"url": e["url"], "status": st})

    # Gerçek konsol hataları (3P gürültü ayıklı)
    real_console_errors = []
    for p in pages:
        for m in p.get("console_messages", []):
            if m["type"] not in ("error", "pageerror"):
                continue
            if any(n.lower() in m["text"].lower() for n in CONSOLE_NOISE):
                continue
            real_console_errors.append({"page": p["url"], "type": m["type"], "text": m["text"]})

    return {
        "base_url": findings["base_url"],
        "unique_paths_crawled": len(pages),
        "max_pages_limit_reached": findings.get("max_pages_limit_reached"),
        "auth_required_skipped": sum(1 for p in pages if p.get("auth_required")),
        "downloadable_files": findings.get("downloadable_files", []),
        "broken_targets": broken_targets,
        "site_broken_resources": list(site_resources.values()),
        "external_dependency_observations": list(external_observations.values()),
        "real_console_errors": real_console_errors,
    }


SYSTEM_PROMPT = """Kıdemli bir QA mühendisi gibi davran. Sana bir Laravel 12 web
uygulamasının salt-okunur tarama bulgularının kompakt bir özeti (JSON) verilecek.

Görevin: bulguları analiz edip TEMİZ, BAŞLIKLI bir Markdown raporu üret. Şunları yap:
1. Bulguları önem sırasına diz: ## Kritik / ## Orta / ## Düşük başlıkları altında.
2. Mümkün olan her yerde KÖK NEDEN açıkla (ör. bir 500'ün veritabanında eksik bir
   kolondan kaynaklanması → muhtemelen eksik/uygulanmamış bir migration).
3. Her bulgu için SOMUT bir düzeltme önerisi ver.
4. Raporun sonunda "## İyileştirme ve Yeni Özellik Fikirleri" başlığı altında
   birkaç fikir sun.

Sadece verilen bulgulara dayan; veri uydurma. Türkçe yaz. Çıktı yalnızca Markdown olsun."""


def call_anthropic(api_key, summary):
    """API'yi çağır. (markdown_text, usage) döner. Hata fırlatabilir."""
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    user_content = (
        "Aşağıdaki tarama bulgularını analiz et ve raporu üret.\n\n"
        "```json\n" + json.dumps(summary, indent=2, ensure_ascii=False) + "\n```"
    )
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )
    text = "".join(b.text for b in response.content if b.type == "text")
    return text, response.usage


def local_fallback_report(summary):
    """API olmadan findings özetinden basit bir Markdown raporu üret."""
    lines = []
    lines.append("# QA Raporu (yerel — yapay zeka olmadan üretildi)")
    lines.append("")
    lines.append(f"- Base URL: {summary['base_url']}")
    lines.append(f"- Gezilen benzersiz path: {summary['unique_paths_crawled']}")
    lines.append(f"- Auth nedeniyle atlanan: {summary['auth_required_skipped']}")
    lines.append("")

    lines.append("## Kırık Hedefler")
    if summary["broken_targets"]:
        for t in summary["broken_targets"]:
            loc = "iç" if t["internal"] else "dış"
            lines.append(f"- [{t['status']}] ({loc}) {t['url']} — {t['reference_count']} referans")
            if t.get("exception_class"):
                lines.append(f"    - İstisna: {t['exception_class']} | {t.get('exception_message','')}")
    else:
        lines.append("- Yok")
    lines.append("")

    lines.append("## Gerçek Site Kırık Kaynakları")
    if summary["site_broken_resources"]:
        for e in summary["site_broken_resources"]:
            lines.append(f"- [{e['status']}] {e['url']} ({e.get('resource_type')})")
    else:
        lines.append("- Yok")
    lines.append("")

    lines.append("## Gerçek Konsol Hataları")
    if summary["real_console_errors"]:
        for m in summary["real_console_errors"]:
            lines.append(f"- [{m['page']}] ({m['type']}) {m['text']}")
    else:
        lines.append("- Yok")
    lines.append("")

    lines.append("## Dış Bağımlılık Gözlemleri")
    if summary["external_dependency_observations"]:
        for e in summary["external_dependency_observations"]:
            lines.append(f"- [{e['status']}] {e['url']}")
    else:
        lines.append("- Yok")
    lines.append("")

    return "\n".join(lines)


def main():
    # findings.json var mı?
    if not FINDINGS_PATH.exists():
        print(f"[HATA] {FINDINGS_PATH} bulunamadı.")
        print("       Önce 'python runner.py' ile Faz 1 taramasını çalıştır.")
        sys.exit(1)

    findings = json.loads(FINDINGS_PATH.read_text(encoding="utf-8"))
    summary = distill(findings)

    api_key = load_api_key()
    if api_key is None:
        print("[HATA] ANTHROPIC_API_KEY .env'de yok veya geçersiz.")
        print("       console.anthropic.com'dan bir anahtar al ve .env'e ekle.")
        print("       (.env.example dosyasına bak.)\n")
        print("       Yine de yerel (yapay zeka olmadan) bir rapor üretiliyor...")
        REPORT_PATH.write_text(local_fallback_report(summary), encoding="utf-8")
        print(f"Yerel rapor yazıldı: {REPORT_PATH}")
        sys.exit(1)

    # API'yi çağır; başarısız olursa yerel rapora düş.
    try:
        import anthropic
        markdown, usage = call_anthropic(api_key, summary)
        REPORT_PATH.write_text(markdown, encoding="utf-8")
        print(f"AI raporu yazıldı: {REPORT_PATH}\n")

        # Token kullanımı + kabaca maliyet
        in_tok = usage.input_tokens
        out_tok = usage.output_tokens
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cost = (in_tok / 1_000_000) * PRICE_INPUT_PER_M + (out_tok / 1_000_000) * PRICE_OUTPUT_PER_M
        print("--- Token kullanımı / maliyet ---")
        print(f"  Model        : {MODEL}")
        print(f"  Girdi token  : {in_tok}  (cache okuma: {cache_read}, cache yazma: {cache_write})")
        print(f"  Çıktı token  : {out_tok}")
        print(f"  Kabaca maliyet: ${cost:.5f}  (${PRICE_INPUT_PER_M}/1M girdi, ${PRICE_OUTPUT_PER_M}/1M çıktı)")
    except anthropic.AuthenticationError:
        print("[HATA] API kimlik doğrulama başarısız (anahtar geçersiz/iptal edilmiş).")
        _write_fallback(summary)
    except anthropic.RateLimitError:
        print("[HATA] API kota/oran sınırına takıldı (429).")
        _write_fallback(summary)
    except anthropic.APIConnectionError as e:
        print(f"[HATA] API'ye bağlanılamadı (ağ): {e}")
        _write_fallback(summary)
    except anthropic.APIStatusError as e:
        print(f"[HATA] API hatası (status {e.status_code}): {e.message}")
        _write_fallback(summary)


def _write_fallback(summary):
    print("       Yapay zeka olmadan yerel rapor üretiliyor...")
    REPORT_PATH.write_text(local_fallback_report(summary), encoding="utf-8")
    print(f"Yerel rapor yazıldı: {REPORT_PATH}")


if __name__ == "__main__":
    main()
