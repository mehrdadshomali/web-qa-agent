#!/usr/bin/env python3
"""
web-qa-agent — Faz 4: tara -> analiz zinciri (ince sarmalayıcı).

Tek komutla önce runner.py (salt-okunur tarama) ardından analyze.py (AI raporu)
çalıştırır. runner.py / analyze.py MANTIĞINA DOKUNMAZ; onları subprocess ile çağırır.

Neden subprocess (import değil): runner/analyze'ın main()'leri kendi argparse'ını
okuyup sys.exit() çağırır ve değer döndürmez; subprocess bunları olduğu gibi
kullanır, süreç izolasyonu + net exit-code verir (scheduler/Telegram için sağlam).

Zincir kuralı: tarama başarısız olursa (site kapalı / login başarısız) analiz
adımına GEÇİLMEZ — yarım veriyle API'ye gidip para harcanmaz.

Hem CLI'dan (main) hem programatik olarak (run_qa) çağrılabilir.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).parent
RUNNER = BASE / "runner.py"
ANALYZE = BASE / "analyze.py"
FINDINGS = BASE / "reports" / "findings.json"
REPORT = BASE / "reports" / "report.md"


def _read_scan_stats():
    """findings.json'dan sayfa sayısı + login durumunu oku (best-effort)."""
    try:
        d = json.loads(FINDINGS.read_text(encoding="utf-8"))
        return {"pages": d.get("pages_visited"), "login": d.get("login")}
    except (OSError, ValueError):
        return {"pages": None, "login": None}


def _count_critical():
    """report.md'nin '## Kritik' bölümündeki '###' başlıklarını say (best-effort)."""
    try:
        text = REPORT.read_text(encoding="utf-8")
    except OSError:
        return None
    in_crit = False
    count = 0
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("## "):
            in_crit = s.startswith("## Kritik")
            continue
        if in_crit and s.startswith("### "):
            count += 1
    return count


def _parse_cost(stdout):
    """analyze.py çıktısındaki 'Kabaca maliyet: $X' satırından maliyeti çıkar."""
    m = re.search(r"maliyet:\s*\$([0-9.]+)", stdout)
    return float(m.group(1)) if m else None


def run_qa(url="http://localhost:8080", max_pages=200, login=True):
    """Tara -> analiz zincirini çalıştır. Sonuç özetini dict olarak döner.

    Dönen dict: {ok, stage, pages, login, critical, report_path, cost_usd}
    ok=False ise stage ('scan' | 'analyze') hangi adımın başarısız olduğunu söyler.
    """
    # --- [1/2] Tarama ---
    scan_cmd = [sys.executable, str(RUNNER), "--url", url, "--max-pages", str(max_pages)]
    if login:
        scan_cmd.append("--login")
    # flush=True: alt-süreç stdout'a doğrudan yazdığı için, kendi satırlarımızı
    # ondan ÖNCE emmeye zorlarız (aksi halde log sırası karışır).
    print(f"[1/2] Tarama başladı... (url={url}, max-pages={max_pages}, "
          f"login={'açık' if login else 'kapalı'})", flush=True)
    scan = subprocess.run(scan_cmd, cwd=str(BASE))  # canlı akış (yakalanmaz)
    if scan.returncode != 0:
        print("\n[1/2] Tarama BAŞARISIZ (site kapalı ya da login başarısız).")
        print("      Analiz adımı ATLANDI — API çağrısı yapılmadı, para harcanmadı.")
        return {"ok": False, "stage": "scan", "pages": None, "login": None,
                "critical": None, "report_path": None, "cost_usd": None}

    stats = _read_scan_stats()
    print(f"[1/2] Tarama bitti: {stats['pages']} sayfa "
          f"(login: {stats['login']}).", flush=True)

    # --- [2/2] AI analizi ---
    print("[2/2] AI analizi başladı...", flush=True)
    analyze = subprocess.run([sys.executable, str(ANALYZE)], cwd=str(BASE),
                             capture_output=True, text=True)
    if analyze.stdout:
        print(analyze.stdout, end="" if analyze.stdout.endswith("\n") else "\n")
    if analyze.stderr:
        print(analyze.stderr, file=sys.stderr, end="")
    if analyze.returncode != 0:
        print("\n[2/2] AI analizi BAŞARISIZ (anahtar/kota/ağ). "
              "Yerel fallback raporu üretilmiş olabilir; yukarıdaki mesaja bakın.")
        return {"ok": False, "stage": "analyze", "pages": stats["pages"],
                "login": stats["login"], "critical": None,
                "report_path": str(REPORT), "cost_usd": None}

    cost = _parse_cost(analyze.stdout or "")
    critical = _count_critical()
    print("[2/2] AI analizi bitti.")

    # --- Özet ---
    print("\n" + "=" * 60)
    print("QA ZİNCİRİ ÖZETİ")
    print("=" * 60)
    print(f"  Taranan sayfa   : {stats['pages']}  (login: {stats['login']})")
    print(f"  Kritik bulgu    : {critical if critical is not None else '?'}")
    print(f"  Rapor           : {REPORT}")
    print(f"  API maliyeti    : "
          f"{('$%.5f' % cost) if cost is not None else 'bilinmiyor'}")
    print("=" * 60)

    return {"ok": True, "stage": "done", "pages": stats["pages"],
            "login": stats["login"], "critical": critical,
            "report_path": str(REPORT), "cost_usd": cost}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="web-qa-agent — tara -> analiz zinciri (tek komut)")
    parser.add_argument("--url", default="http://localhost:8080",
                        help="Base URL (varsayılan: http://localhost:8080)")
    parser.add_argument("--max-pages", type=int, default=200,
                        help="Gezilecek maksimum sayfa (varsayılan: 200)")
    parser.add_argument("--no-login", action="store_true",
                        help="Login'siz (yalnızca guest) tara. Varsayılan: login açık.")
    args = parser.parse_args(argv)

    result = run_qa(url=args.url, max_pages=args.max_pages, login=not args.no_login)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
