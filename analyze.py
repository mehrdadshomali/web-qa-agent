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

from code_context import collect_code_context

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 8192
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

# En fazla kaç farklı 500 için kod kanıtı toplanır (prompt şişmesin).
CODE_EVIDENCE_MAX_500 = 5


def collect_code_evidence(findings, target_repo_path):
    """Her benzersiz 500 için code_context.py'den (salt-okuma) kanıt topla.

    target_repo_path yok/erişilemezse boş liste döner (kod erişimi opsiyoneldir;
    bu durumda analyze eski 'tahmin' davranışına düşer). Her 500'ün kod bağlamı
    code_context tarafından ~birkaç KB ile sınırlanır.
    """
    if not target_repo_path:
        return []
    evidence, seen = [], set()
    for p in findings.get("pages", []):
        if not (p.get("status") and p["status"] >= 500 and p.get("exception")):
            continue
        url = p["url"]
        if url in seen:
            continue
        seen.add(url)
        exc = p["exception"]
        message = exc.get("message") or exc.get("title") or ""
        ctx = collect_code_context(message, target_repo_path)
        if ctx.get("available") and ctx.get("snippets"):
            evidence.append({
                "url": url,
                "status": p["status"],
                "exception_class": exc.get("exception_class"),
                "code_root": ctx.get("code_root"),
                "search_terms": ctx.get("terms"),
                "snippets": ctx["snippets"],
                "truncated": ctx.get("truncated"),
            })
        if len(evidence) >= CODE_EVIDENCE_MAX_500:
            break
    return evidence


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

    # --- Erişilebilirlik: KURAL bazında topla (ham node listeleri GÖNDERİLMEZ) ---
    a11y_rules = {}
    for p in pages:
        for v in (p.get("accessibility") or []):
            r = a11y_rules.setdefault(v["id"], {
                "rule": v["id"],
                "impact": v.get("impact"),
                "description": v.get("description"),
                "total_nodes": 0,
                "pages_affected": 0,
            })
            r["total_nodes"] += v.get("nodes", 0)
            r["pages_affected"] += 1
    _impact_order = {"critical": 0, "serious": 1, "moderate": 2, "minor": 3}
    a11y_rules_sorted = sorted(
        a11y_rules.values(),
        key=lambda r: (_impact_order.get(r["impact"], 9), -r["total_nodes"]),
    )
    by_impact = {"critical": 0, "serious": 0, "moderate": 0, "minor": 0, "other": 0}
    for r in a11y_rules_sorted:
        imp = r["impact"] if r["impact"] in by_impact else "other"
        by_impact[imp] += r["pages_affected"]
    accessibility_summary = {
        "total_violations": sum(r["pages_affected"] for r in a11y_rules_sorted),
        "by_impact": by_impact,
        "rules": a11y_rules_sorted,  # önem + yaygınlık sırasında, en fazla ~12 kural
    }

    # --- Performans: yalnızca en ağır 5 sayfa + genel ortalamalar ---
    perf = [(p["url"], p["performance"]) for p in pages
            if p.get("performance") and p["performance"].get("transferred_bytes")]
    loads = [pf["load_ms"] for _, pf in perf if pf.get("load_ms")]
    performance_summary = {
        "pages_measured": len(perf),
        "avg_load_ms": round(sum(loads) / len(loads)) if loads else None,
        "total_transferred_mb": round(sum(pf["transferred_bytes"] for _, pf in perf) / 1024 / 1024, 1),
        "heaviest_pages": [
            {"url": u, "transferred_kb": round(pf["transferred_bytes"] / 1024),
             "requests": pf.get("request_count"), "load_ms": pf.get("load_ms")}
            for u, pf in sorted(perf, key=lambda x: -x[1]["transferred_bytes"])[:5]
        ],
    }

    return {
        "base_url": findings["base_url"],
        "scan_date": findings.get("generated_at"),  # gerçek tarama tarihi (ISO); rapor bunu kullanmalı
        "unique_paths_crawled": len(pages),
        "max_pages_limit_reached": findings.get("max_pages_limit_reached"),
        "auth_required_skipped": sum(1 for p in pages if p.get("auth_required")),
        "downloadable_files": findings.get("downloadable_files", []),
        "broken_targets": broken_targets,
        "site_broken_resources": list(site_resources.values()),
        "external_dependency_observations": list(external_observations.values()),
        "real_console_errors": real_console_errors,
        "accessibility_summary": accessibility_summary,
        "performance_summary": performance_summary,
    }


SYSTEM_PROMPT = """Kıdemli bir QA mühendisi gibi davran. Sana bir Laravel 12 web
uygulamasının salt-okunur tarama bulgularının kompakt bir özeti (JSON) verilecek.

Görevin: bulguları analiz edip TEMİZ, BAŞLIKLI bir Markdown raporu üret. Şunları yap:
1. Bulguları önem sırasına diz: ## Kritik / ## Orta / ## Düşük başlıkları altında.
   Kırık sayfalar/kaynaklar, ERİŞİLEBİLİRLİK ihlalleri ve PERFORMANS sorunlarının
   HEPSİNİ bu önem seviyelerine dahil et (ör. site genelinde critical a11y ihlali =
   Kritik/Orta; ağır bir sayfa = Orta/Düşük).
2. KÖK NEDEN çıkarımı yap ama bunun bir TAHMİN olduğunu dille belli et
   ("muhtemelen", "olası neden", "doğrulanması gerekir"). Aşağıdaki
   "KESİN TESPİT vs TAHMİN" kuralına harfiyen uy.
3. Her bulgu için SOMUT bir düzeltme önerisi ver.
   - Öneri bir TAHMİNE dayanıyorsa başına kısa bir "Önce doğrula" adımı koy
     (ör. "Önce N+1 olup olmadığını Laravel Debugbar/Telescope ile doğrulayın, sonra...").
   - Erişilebilirlik için: kısa WCAG bağlamı ver (hangi başarı kriteri) ve pratik
     düzeltme (ör. button-name → ikon-butonlara aria-label; image-alt → alt metni;
     color-contrast → kontrast oranını 4.5:1'e çıkar). Kural site genelinde
     tekrarlıyorsa (çok sayfada) bunu tema/bileşen düzeyinde tek düzeltme olarak öner.
   - Performans için: somut öneri ver (ör. ana sayfa 7.8 MB → görselleri optimize
     et/WebP + lazy-load, Three.js/GSAP paketini böl, gereksiz istekleri azalt) —
     ancak ağırlığın SEBEBİ ölçülmediği için önce "sayfayı ne şişiriyor" doğrulanmalı.
4. Raporun sonunda "## İyileştirme ve Yeni Özellik Fikirleri" başlığı altında
   birkaç fikir sun.

KESİN TESPİT vs TAHMİN (çok önemli — ton kuralı):
- TESPİTLER = findings.json'daki ÖLÇÜLEN veriler: HTTP durum kodları, hata
  mesajları/istisna sınıfları, axe erişilebilirlik ihlalleri, sayfa ağırlığı (byte),
  istek sayısı, yükleme süresi. Bunlar KESİN gerçeklerdir; kesin dille yaz.
- KÖK NEDENLER ÇIKARIMDIR. Ajan sayfa ağırlığını ÖLÇTÜ ama SEBEBİNİ (Three.js mi,
  büyük görsel mi, N+1 sorgu mu) DOĞRULAMADI. Kök nedenleri ASLA kesin dille yazma.
- Ajan yalnızca DIŞARIDAN (tarayıcı) gözlem yaptı; KAYNAK KODA veya VERİTABANINA
  BAKMADI. Sunucu-tarafı tahminlerini (N+1 sorgu, migration durumu, git deployment,
  hangi JS kütüphanesinin yüklü olduğu vb.) "kontrol edilmeli" çerçevesinde öner;
  "şöyledir/şundandır" diye kesin ifade ETME.

İSTİSNA — 'code_evidence' (kod-destekli teşhis):
- Girdide bir 500 için 'code_evidence' verildiyse (o hataya ait GERÇEK kaynak koddan
  okunmuş dosya/satır parçaları), o 500'ün teşhisini ARTIK TAHMİN DEĞİL kabul et:
  KESİN dille yaz (yani "muhtemelen"/"doğrulanmalı" KULLANMA) ve teşhisini hangi
  DOSYA:SATIR'a dayandırdığını AÇIKÇA belirt.
- Bu kod-kanıtlı 500 teşhisinde şunları ver:
  (a) hangi dosyadaki hangi sorgu/kod sorunu yaratıyor,
  (b) NEDEN (ör. bir kolon Schema::hasColumn guard'ı OLMADAN kullanılıyor, oysa
      aynı sorgudaki diğer kolonlar guard ile korunuyor; ya da model kolonu bekliyor
      ama migration eklememiş),
  (c) İKİ somut çözüm: (1) sorguyu guard'la (ör. eksik kolonu Schema::hasColumn ile
      koru) VEYA (2) kolonu bir migration ile tabloya ekle.
- 'code_evidence' VERİLMEYEN 500'lerde ve diğer tüm bulgularda yukarıdaki temkinli
  ('muhtemelen', 'doğrulanmalı') dil aynen geçerlidir. Yani: kanıt varsa KESİN,
  yoksa TEMKİNLİ.

Girdideki 'accessibility_summary' kural bazında damıtılmıştır (rule id + impact +
toplam öğe + kaç sayfada); ham node listesi yoktur. 'performance_summary' yalnızca
en ağır 5 sayfayı ve genel ortalamaları içerir. Bunları olduğu gibi kullan.

'code_evidence' (varsa) her kod-destekli 500 için şu yapıdadır:
{url, status, exception_class, code_root, snippets:[{file, line, terms, context}]}
— 'context' ilgili kaynak kodun numaralı satırlarıdır. Teşhisi bunlara dayandır.

Rapor tarihini sana verilen gerçek 'scan_date' değerinden al (ISO tarih-saat; yalnızca
tarih kısmını göstermen yeterli), ASLA uydurma. scan_date yoksa tarih satırı ekleme.

Raporun EN BAŞINA (ana başlık ve tarih/özet satırlarından hemen sonra) şu notu AYNEN ekle:
> **Not:** Tespitler otomatik tarama ile ölçülmüştür ve kesindir. Kök neden analizleri
> ve düzeltme önerileri, dış gözleme dayalı çıkarımlardır; uygulanmadan önce ilgili
> geliştirici tarafından doğrulanmalıdır. Kod kanıtına dayalı teşhisler ise ilgili
> kaynak koddan doğrulanmıştır.

Sadece verilen bulgulara dayan; veri uydurma. Türkçe yaz. Çıktı yalnızca Markdown olsun."""


def call_anthropic(api_key, summary):
    """API'yi çağır. (markdown_text, usage) döner. Hata fırlatabilir.

    Mutlak timeout (180s) + azaltılmış retry ile ağ stall'ında sonsuz asılı
    kalmaz; süre aşımında APITimeoutError fırlar ve çağıran taraf yerel rapora
    düşer (her halükârda çıktı üretilir). 180s, gözlenen ~110s'lik gerçek çağrı
    süresine güvenli marj bırakır.
    """
    import time

    import anthropic

    client = anthropic.Anthropic(api_key=api_key, timeout=180.0, max_retries=1)
    user_content = (
        "Aşağıdaki tarama bulgularını analiz et ve raporu üret.\n\n"
        "```json\n" + json.dumps(summary, indent=2, ensure_ascii=False) + "\n```"
    )
    print("API çağrılıyor... (timeout=180s, max_retries=1)")
    t0 = time.monotonic()
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
    finally:
        print(f"API çağrısı {time.monotonic() - t0:.1f} saniye sürdü.")
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

    a11y = summary.get("accessibility_summary") or {}
    lines.append("## Erişilebilirlik (WCAG A+AA, kural bazında)")
    if a11y.get("rules"):
        bi = a11y.get("by_impact", {})
        lines.append(f"Toplam {a11y.get('total_violations', 0)} ihlal "
                     f"(critical={bi.get('critical',0)}, serious={bi.get('serious',0)}, "
                     f"moderate={bi.get('moderate',0)}, minor={bi.get('minor',0)})")
        for r in a11y["rules"]:
            lines.append(f"- **{r['rule']}** [{r['impact']}] — {r['pages_affected']} sayfada, "
                         f"{r['total_nodes']} öğe — {r.get('description','')}")
    else:
        lines.append("- Yok")
    lines.append("")

    perf = summary.get("performance_summary") or {}
    lines.append("## Performans (en ağır sayfalar)")
    if perf.get("heaviest_pages"):
        lines.append(f"Ortalama yükleme {perf.get('avg_load_ms')} ms | toplam "
                     f"{perf.get('total_transferred_mb')} MB ({perf.get('pages_measured')} sayfa)")
        for hp in perf["heaviest_pages"]:
            lines.append(f"- {hp['transferred_kb']} KB | {hp['requests']} istek | "
                         f"{hp['load_ms']} ms — {hp['url']}")
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

    # distill()'den SONRA, API çağrısından ÖNCE: her 500 için kaynak koddan kanıt
    # topla (salt-okuma). TARGET_REPO_PATH yoksa atlanır -> eski 'tahmin' davranışı.
    target_repo = os.environ.get("TARGET_REPO_PATH", "").strip()
    summary["code_evidence"] = collect_code_evidence(findings, target_repo)
    if summary["code_evidence"]:
        print(f"[kod kanıtı] {len(summary['code_evidence'])} adet 500 için kaynak "
              f"koddan kanıt toplandı (TARGET_REPO_PATH ayarlı).")
    elif target_repo:
        print("[kod kanıtı] TARGET_REPO_PATH ayarlı ama 500 için kanıt bulunamadı.")
    else:
        print("[kod kanıtı] TARGET_REPO_PATH ayarlı değil — kanıt atlandı (tahmin modu).")

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
