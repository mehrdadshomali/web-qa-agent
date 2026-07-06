#!/usr/bin/env python3
"""
web-qa-agent — Faz 5(a): 500 hataları için kod-destekli teşhis (bağlam toplama).

collect_code_context(error_message, target_repo_path):
  Bir 500 hata mesajından (özellikle Laravel QueryException) anahtar terimleri
  çıkarır ve HEDEF UYGULAMANIN kaynak kodunda (salt-okuma) ilgili kod parçalarını
  bulup döndürür.

Strateji (spesifiklik-öncelikli, iki geçiş):
  - Terimler AYRILIR:
      * yüksek-spesifik = "Unknown column"dan gelen kolon (student_club_id),
        FROM/JOIN'deki tablo (brand_opportunities) ve tablodan türetilen model
        sınıfı (BrandOpportunity). Bunlar gerçek sinyaldir.
      * jenerik = status/type/is_active/is_approved gibi her yerde geçen kolonlar.
  - 1. GEÇİŞ: yalnızca yüksek-spesifik terimler; dizinler öncelik sırasıyla taranır
    (database/migrations -> app/Models -> app -> database -> routes). Co-occurrence
    (bir parçada birden çok spesifik terim) en öne alınır.
  - 2. GEÇİŞ: bütçe kalırsa jenerik terimler.

GÜVENLİK (bu modül asla değiştirilmemeli):
  - SADECE OKUMA. Hiçbir dosya yazma/silme, hiçbir komut çalıştırma YOK.
  - Yalnızca target_repo_path altındaki app/ database/ routes/ taranır;
    vendor/ node_modules/ storage/ .git/ public/ config/ HARİÇ.
  - Hassas dosyalar (.env*, adında password/secret geçenler) OKUNMAZ.
  - Toplam çıktı birkaç KB ile sınırlıdır (API'ye tüm kod gitmesin).
  - target_repo_path yok/erişilemezse boş sonuç döner, hata fırlatmaz.

Bu modül tek başına test edilebilir (aşağıdaki main). API çağrısı YAPMAZ.
"""

import os
import re
import sys
from pathlib import Path

# --- Güvenlik/sınır ayarları ---
CODE_DIRS = ("app", "database", "routes")
EXCLUDE_DIRS = {
    "vendor", "node_modules", "storage", ".git", ".idea", "public",
    "bootstrap", "config", "tests", "lang", "resources",
}
# Tarama önceliği: migration'lar (kolon tanımlı mı?) ve modeller önce.
PRIORITY_PREFIXES = ["database/migrations", "app/Models", "app", "database", "routes"]

MAX_HIGH = 6                 # en fazla kaç yüksek-spesifik terim
MAX_GENERIC = 4              # en fazla kaç jenerik terim
MAX_SNIPPETS = 8
CONTEXT_BEFORE = 4
CONTEXT_AFTER = 6
MAX_FILE_BYTES = 500_000
MAX_TOTAL_BYTES = 6_000

STOPWORDS = {
    "select", "count", "from", "where", "and", "or", "as", "is", "not", "null",
    "aggregate", "mysql", "host", "port", "connection", "database", "inner",
    "join", "on", "order", "by", "limit", "offset", "insert", "into", "update",
    "set", "values", "distinct", "exists", "in", "asc", "desc", "left", "right",
    "true", "false", "published", "active", "sqlstate", "column", "unknown",
    "table", "row", "rows", "query", "sql",
}


def _singularize(word):
    w = word
    if w.endswith("ies"):
        return w[:-3] + "y"
    for suf in ("ses", "xes", "zes", "ches", "shes"):
        if w.endswith(suf):
            return w[:-2]
    if w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def _table_to_model(table):
    """Laravel kuralı: brand_opportunities -> BrandOpportunity (tekil + StudlyCase)."""
    parts = table.split("_")
    if parts:
        parts[-1] = _singularize(parts[-1])
    return "".join(p[:1].upper() + p[1:] for p in parts if p)


def _extract_terms(error_message):
    """(yüksek_spesifik, jenerik) terim listelerini döndür."""
    msg = error_message or ""
    error_cols = [m.group(1).split(".")[-1]
                  for m in re.finditer(r"[Uu]nknown column '([^']+)'", msg)]
    tables = [m.group(1) for m in
              re.finditer(r"\b(?:from|join|into|update)\s+`?([A-Za-z0-9_]+)`?", msg, re.IGNORECASE)]
    backticks = [m.group(1) for m in re.finditer(r"`([A-Za-z0-9_]+)`", msg)]
    models = [_table_to_model(t) for t in tables]

    high, generic, seen = [], [], set()

    def add(lst, t):
        t = t.strip()
        if len(t) < 3 or t.lower() in STOPWORDS or t.lower() in seen:
            return
        seen.add(t.lower())
        lst.append(t)

    for t in error_cols:
        add(high, t)
    for t in tables:
        add(high, t)
    for t in models:
        add(high, t)
    for t in backticks:        # tablolar zaten seen'de -> tekrar eklenmez
        add(generic, t)

    return high[:MAX_HIGH], generic[:MAX_GENERIC]


def _resolve_code_root(target_repo_path):
    root = Path(target_repo_path)
    if not root.is_dir():
        return None
    if (root / "app").is_dir():
        return root
    if (root / "src" / "app").is_dir():
        return root / "src"
    return None


def _is_sensitive(filename):
    low = filename.lower()
    return low.startswith(".env") or "password" in low or "secret" in low


def _list_files(code_root):
    """(priority_index, rel_path, abs_path) — öncelik sırasında dosyalar."""
    out = []
    for base in CODE_DIRS:
        d = code_root / base
        if not d.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(d):
            dirnames[:] = [x for x in dirnames if x not in EXCLUDE_DIRS]
            for fn in filenames:
                if not fn.endswith(".php") or _is_sensitive(fn):
                    continue
                f = Path(dirpath) / fn
                rel = str(f.relative_to(code_root)).replace(os.sep, "/")
                pidx = next((i for i, p in enumerate(PRIORITY_PREFIXES) if rel.startswith(p)),
                            len(PRIORITY_PREFIXES))
                out.append((pidx, rel, f))
    out.sort(key=lambda x: (x[0], x[1]))
    return out


def _gather_candidates(files, terms):
    """Öncelik-sıralı dosyalarda terimleri ara; pencere + co-occurrence üret."""
    if not terms:
        return []
    candidates = []
    for pidx, rel, f in files:
        try:
            if f.stat().st_size > MAX_FILE_BYTES:
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        hits = [(i, [t for t in terms if t in ln])
                for i, ln in enumerate(lines) if any(t in ln for t in terms)]
        if not hits:
            continue
        # yakın eşleşmeleri tek pencerede birleştir
        wins = []
        for i, matched in hits:
            start = max(0, i - CONTEXT_BEFORE)
            end = min(len(lines), i + CONTEXT_AFTER + 1)
            if wins and start <= wins[-1]["end"]:
                wins[-1]["end"] = max(wins[-1]["end"], end)
                wins[-1]["terms"].update(matched)
            else:
                wins.append({"start": start, "end": end, "terms": set(matched)})
        for w in wins:
            ctx = "\n".join(f"{n + 1}: {lines[n]}" for n in range(w["start"], w["end"]))
            candidates.append({
                "file": rel, "line": w["start"] + 1, "terms": sorted(w["terms"]),
                "cooccur": len(w["terms"]), "priority": pidx, "context": ctx,
            })
    # Sıralama: önce co-occurrence (çok terimli = gerçek kanıt), sonra dizin önceliği.
    candidates.sort(key=lambda c: (-c["cooccur"], c["priority"], c["file"], c["line"]))
    return candidates


def _emit(candidates, snippets, total_bytes, seen_windows):
    """Adayları bütçe/tekrar sınırlarına uyarak snippets'e ekle."""
    truncated = False
    for c in candidates:
        if len(snippets) >= MAX_SNIPPETS or total_bytes >= MAX_TOTAL_BYTES:
            truncated = True
            break
        key = (c["file"], c["line"] // 5)
        if key in seen_windows:
            continue
        if total_bytes + len(c["context"]) > MAX_TOTAL_BYTES:
            truncated = True
            continue
        seen_windows.add(key)
        snippets.append(c)
        total_bytes += len(c["context"])
    return total_bytes, truncated


def collect_code_context(error_message, target_repo_path):
    """500 hata mesajı + hedef repo yolu -> ilgili kod parçaları (salt-okuma)."""
    if not target_repo_path:
        return {"available": False, "reason": "TARGET_REPO_PATH tanımlı değil.",
                "terms": {"specific": [], "generic": []}, "snippets": [], "truncated": False}

    code_root = _resolve_code_root(target_repo_path)
    if code_root is None:
        return {"available": False,
                "reason": f"Kod kökü bulunamadı (app/ yok): {target_repo_path}",
                "terms": {"specific": [], "generic": []}, "snippets": [], "truncated": False}

    high, generic = _extract_terms(error_message)
    if not high and not generic:
        return {"available": True, "reason": "Mesajdan aranacak terim çıkarılamadı.",
                "code_root": str(code_root), "terms": {"specific": [], "generic": []},
                "snippets": [], "truncated": False}

    files = _list_files(code_root)
    snippets, total, seen = [], 0, set()

    # 1. GEÇİŞ — yüksek-spesifik (migration/model öncelikli, co-occurrence sıralı)
    total, trunc1 = _emit(_gather_candidates(files, high), snippets, total, seen)

    # 2. GEÇİŞ — jenerik (yalnızca bütçe kaldıysa)
    trunc2 = False
    if len(snippets) < MAX_SNIPPETS and total < MAX_TOTAL_BYTES:
        total, trunc2 = _emit(_gather_candidates(files, generic), snippets, total, seen)

    return {"available": True, "reason": None, "code_root": str(code_root),
            "terms": {"specific": high, "generic": generic},
            "snippets": snippets, "truncated": trunc1 or trunc2}


# --- Tek başına test (API çağrısı YOK; sadece kod okuma) ---------------------

def _first_500_message():
    import json
    path = Path(__file__).parent / "reports" / "findings.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    for p in data.get("pages", []):
        if p.get("status") and p["status"] >= 500 and p.get("exception"):
            exc = p["exception"]
            return exc.get("message") or exc.get("title") or ""
    return None


def main():
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
    target = os.environ.get("TARGET_REPO_PATH", "").strip()

    msg = _first_500_message()
    if not msg:
        print("findings.json'da 500 istisna mesajı bulunamadı. Önce runner.py çalıştırın.")
        sys.exit(0)

    print("=== 500 hata mesajı ===")
    print(msg[:300] + ("..." if len(msg) > 300 else ""))
    print("\n=== TARGET_REPO_PATH ===")
    print(repr(target) or "(boş)")

    result = collect_code_context(msg, target)
    print("\n=== SONUÇ ===")
    print("available :", result["available"], "| reason:", result["reason"])
    print("code_root :", result.get("code_root"))
    print("terms.specific :", result["terms"]["specific"])
    print("terms.generic  :", result["terms"]["generic"])
    print("truncated :", result["truncated"])
    print(f"\n=== BULUNAN KOD PARÇALARI ({len(result['snippets'])}) — co-occurrence sıralı ===")
    for s in result["snippets"]:
        print(f"\n--- {s['file']}:{s['line']}  "
              f"(eşleşen [{s['cooccur']}]: {', '.join(s['terms'])}) ---")
        print(s["context"])


if __name__ == "__main__":
    main()
