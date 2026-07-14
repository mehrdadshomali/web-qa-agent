#!/usr/bin/env python3
"""
flow_tests/ticket_cart_flow.py — İlk E2E akışı (AKTİF aksiyon: tıklama + form).

Akış (plandaki 5 adım):
    login -> dinamik uygun etkinlik -> +1 bilet -> "Sepete Ekle" -> /sepet -> +1 adet -> doğrula

Güvenli-varsayılan: SADECE local + mail=log + ödeme=fake ortamında çalışır. Her
çalıştırmanın başında güvenlik ön koşulu doğrulanır; sağlanmazsa çalışma REDDEDİLİR.

runner.py'nin do_login / sabit / .env-yükleme mantığını YENİDEN KULLANIR ama onu
DEĞİŞTİRMEZ (bu ayrı bir modüldür). Blade kaynağına da dokunulmaz; selector'lar
mevcut yapıdan (#bilet-al, [x-data^=cartItemCard], .bi-plus) türetilir.

Kullanım:
    python flow_tests/ticket_cart_flow.py                 # görünür (headed) çalışır
    python flow_tests/ticket_cart_flow.py --no-headed     # CI / arka plan
    python flow_tests/ticket_cart_flow.py --target-env /Users/.../badinext2/src/.env

Not: Bu betik kodu yazıp durur; çalıştırma kullanıcı onayıyla yapılır.
"""

import argparse
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

# runner.py proje kökündedir; import edebilmek için kökü path'e ekle.
# (runner import'u yan etki üretmez: main() yalnızca __main__ altında çalışır.)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from runner import do_login, AUTH_URL_PATTERNS, NAV_TIMEOUT_MS, ALPINE_WAIT_MS  # noqa: E402

# Sepet AJAX'ı için ekstra bekleme (PATCH/POST tamamlansın).
AJAX_WAIT_MS = 1500
MAX_EVENT_CANDIDATES = 20  # /etkinlikler listesinden en fazla kaç aday denenir


# --- Çıktı yardımcıları ----------------------------------------------------

def hdr(text):
    print("\n" + "=" * 62 + f"\n{text}\n" + "=" * 62)


def step(n, title):
    print(f"\n[ADIM {n}] {title}")


def passed(results, name, detail):
    print(f"   ✓ GEÇTI — {detail}")
    results.append((name, True, detail))


def failed(results, name, detail):
    print(f"   ✗ KALDI — {detail}")
    results.append((name, False, detail))


# --- Güvenlik ön koşulu ----------------------------------------------------

def read_env_file(path):
    """Basit .env okuyucu: KEY=VALUE satırları, tırnak/yorum temizlenir."""
    vals = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        vals[k.strip()] = v.strip().strip('"').strip("'")
    return vals


def resolve_target_env(explicit):
    """Hedef src/.env yolunu belirle: önce --target-env, sonra web-qa-agent/.env
    içindeki TARGET_REPO_PATH -> <path>/src/.env."""
    if explicit:
        return explicit
    agent_env = ROOT / ".env"
    if agent_env.is_file():
        trp = read_env_file(agent_env).get("TARGET_REPO_PATH", "").strip()
        if trp:
            return str(Path(trp) / "src" / ".env")
    return None


def resolve_target_repo():
    """Hedef Laravel repo kökü (docker-compose.yml burada) — teardown'ın çalışma
    dizini. web-qa-agent/.env içindeki TARGET_REPO_PATH."""
    agent_env = ROOT / ".env"
    if agent_env.is_file():
        trp = read_env_file(agent_env).get("TARGET_REPO_PATH", "").strip()
        if trp:
            return trp
    return None


def check_security_precondition(target_env_path):
    """MAIL_MAILER=log ve PAYMENT_GATEWAY boş/fake mi? Doğrulanamazsa GÜVENLİ-RED.
    (ok: bool, satırlar: list[str]) döner."""
    if not target_env_path or not Path(target_env_path).is_file():
        return False, [
            f"Hedef src/.env bulunamadı: {target_env_path}",
            "Güvenlik ön koşulu DOĞRULANAMADI — çalışma reddedildi.",
            "Çözüm: --target-env ile doğru yolu verin, ör:",
            "  --target-env /Users/mehrdadshomali/Desktop/badinext2/src/.env",
            "(web-qa-agent/.env içindeki TARGET_REPO_PATH yanlış olabilir.)",
        ]
    env = read_env_file(target_env_path)
    mail = env.get("MAIL_MAILER", "")
    pay = env.get("PAYMENT_GATEWAY", "")  # boş = config/payments.php default 'fake'
    problems = []
    if mail != "log":
        problems.append(f"MAIL_MAILER='{mail}' (beklenen: log) — GERÇEK MAIL riski!")
    if pay not in ("", "fake"):
        problems.append(f"PAYMENT_GATEWAY='{pay}' (beklenen: boş veya fake) — GERÇEK ÖDEME riski!")
    if problems:
        return False, ["Güvenlik ön koşulu SAĞLANMADI — çalışma reddedildi:"] + problems + [
            f"(kaynak: {target_env_path})"]
    return True, [
        "MAIL_MAILER=log  ✓  (mailler storage/logs/laravel.log'a gider)",
        f"PAYMENT_GATEWAY={pay or '(boş → fake)'}  ✓  (gerçek ödeme yok)",
        f"kaynak: {target_env_path}",
    ]


# --- Profil ön koşulu ------------------------------------------------------

def check_profile_precondition(context, base_url):
    """Youth login sonrası: hesap korumalı sepet akışına ulaşabiliyor mu, yoksa
    ProfileCompleted middleware profile mi yönlendiriyor?

    /sepet (cart.index) ProfileCompleted grubundadır; bireysel kullanıcıda
    doğrulanmamış e-posta VEYA eksik profil -> dashboard.profile'a redirect.
    (ulasabilir: bool, final_url: str, sebep: str|None) döner.
    """
    page = context.new_page()
    page.set_default_navigation_timeout(NAV_TIMEOUT_MS)
    try:
        page.goto(base_url + "/sepet", wait_until="load")
        page.wait_for_timeout(ALPINE_WAIT_MS)
        final = page.url
    finally:
        pass
    page.close()

    parts = urlparse(final)
    path = parts.path.rstrip("/")
    # tab bilgisi query'de VEYA fragment'te gelebilir (ör. /dashboard#profile:personal)
    marker = (parts.query + " " + parts.fragment).lower()

    # Başarı KRİTERİ: /sepet'te KALDIYSAK profil tamam. Aksi halde yönlendirildik.
    if path == "/sepet":
        return True, final, None
    if AUTH_URL_PATTERNS.search(path):
        return False, final, "login"          # oturum düştü / auth sorunu
    # profile/dashboard'a atıldık: tab=contact -> e-posta, tab=personal -> profil eksik
    if "contact" in marker:
        return False, final, "email"
    if "personal" in marker:
        return False, final, "profile"
    return False, final, "unknown"


def profile_remediation(reason):
    """Setup eksikliği için net yönlendirme metni (bug DEĞİL)."""
    lines = ["Bu bir UYGULAMA HATASI değil — TEST HESABI KURULUM EKSİKLİĞİDİR.", ""]
    if reason == "email":
        lines += [
            "Sebep: youth test hesabının e-postası DOĞRULANMAMIŞ.",
            "Çözüm (badinext2/src içinde tinker ile):",
            "  User::where('email','test-youth@qa.local')->update(['email_verified_at'=>now()]);",
        ]
    elif reason == "profile":
        lines += [
            "Sebep: youth test hesabının PROFİLİ EKSİK (isProfileComplete() false).",
            "Gerekli alanlar: Ad, Soyad, Telefon, KVKK Onayı",
            "  (first_name, last_name, phone, kvkk_consent).",
        ]
    elif reason == "login":
        lines += ["Sebep: /sepet isteği login'e döndü — oturum kurulamadı veya guard reddetti."]
    else:
        lines += [
            "Sebep: profile'a yönlendirildi (tab belirsiz). Muhtemel: e-posta doğrulama",
            "veya eksik profil alanları (Ad/Soyad/Telefon/KVKK).",
        ]
    return lines


# --- Akış selector yardımcıları --------------------------------------------

def qty_before_plus(plus_locator):
    """+/- stepper'ında adet <span> = + butonunun hemen önceki kardeş span'i."""
    return plus_locator.locator("xpath=preceding-sibling::span[1]")


def read_int(locator, default=None):
    try:
        return int((locator.inner_text() or "").strip())
    except (ValueError, PlaywrightError):
        return default


def error_box_visible(scope):
    """scope içinde görünür kırmızı hata kutusu var mı? (Alpine x-text=errorMessage/error)"""
    box = scope.locator("[x-text='errorMessage'], [x-text='error']")
    try:
        return box.count() > 0 and box.first.is_visible()
    except PlaywrightError:
        return False


def find_suitable_event(page, base_url):
    """/etkinlikler listesinden, #bilet-al panelinde satışta bilet (+ butonu) olan
    ilk etkinliğin URL'sini döndür. Yoksa None."""
    page.goto(base_url + "/etkinlikler", wait_until="load")
    page.wait_for_timeout(ALPINE_WAIT_MS)
    hrefs = page.eval_on_selector_all(
        "a[href*='/etkinlikler/']",
        "els => els.map(e => e.getAttribute('href'))",
    )
    seen, candidates = set(), []
    for h in hrefs or []:
        if not h:
            continue
        parts = [p for p in urlparse(h).path.split("/") if p]
        # /etkinlikler/{slug} — tam iki segment; alt-sayfaları (takvim vs) ele
        if len(parts) != 2 or parts[0] != "etkinlikler":
            continue
        slug = parts[1]
        if slug in ("sidebar-takvim",) or slug in seen:
            continue
        seen.add(slug)
        candidates.append(base_url + "/etkinlikler/" + slug)

    for url in candidates[:MAX_EVENT_CANDIDATES]:
        page.goto(url, wait_until="load")
        page.wait_for_timeout(ALPINE_WAIT_MS)
        panel = page.locator("#bilet-al")
        if panel.count() == 0:
            continue  # "Şu an satışta bilet bulunmuyor" — panel render edilmez
        if panel.locator("button:has(i.bi-plus)").count() == 0:
            continue  # bilet türleri var ama hiçbiri satışta değil
        return url
    return None


# --- Teardown: ödenmiş test siparişleri (scoped tinker) --------------------

def teardown_paid_orders(target_repo, youth_email):
    """docker compose exec app php artisan tinker ile, YALNIZCA youth test kullanıcısının
    + QA test etkinliğinin (slug=qa-test-etkinlik) order'larını siler. Biletler FK
    cascade ile otomatik gider. Silme ÇİFT KOŞULLU koda gömülüdür (user_id + event_id)
    ve slug önce doğrulanır; başka kullanıcı/etkinliğe ASLA dokunmaz.
    (ok: bool, mesaj: str) döner. Docker/container hatasında ok=False -> akış durmalı.
    """
    # E-postada tek tırnak olursa PHP string literal'i bozulur -> güvenli-red.
    if "'" in youth_email or "\\" in youth_email:
        return False, f"Güvenlik: e-postada geçersiz karakter, teardown iptal: {youth_email}"

    php = (
        "$e=\\App\\Models\\Event::where('slug','qa-test-etkinlik')->first();"
        "$u=\\App\\Models\\User::where('email','" + youth_email + "')->first();"
        "if(!$e){echo 'TEARDOWN_NO_EVENT';}"
        "elseif($e->slug!=='qa-test-etkinlik'){echo 'TEARDOWN_SLUG_MISMATCH';}"
        "elseif(!$u){echo 'TEARDOWN_NO_USER';}"
        "else{$q=\\App\\Models\\Order::where('user_id',$u->id)->where('event_id',$e->id);"
        "$n=$q->count();$q->delete();echo 'TEARDOWN_OK deleted='.$n;}"
    )
    try:
        proc = subprocess.run(
            ["docker", "compose", "exec", "-T", "app",
             "php", "artisan", "tinker", "--execute", php],
            cwd=target_repo, capture_output=True, text=True, timeout=90,
        )
    except FileNotFoundError:
        return False, "docker bulunamadı (PATH'te 'docker' yok)."
    except subprocess.TimeoutExpired:
        return False, "Teardown zaman aşımı (docker/container yanıt vermedi)."

    out = (proc.stdout or "") + (proc.stderr or "")
    if "TEARDOWN_OK" in out:
        m = re.search(r"TEARDOWN_OK deleted=(\d+)", out)
        return True, f"{m.group(1) if m else '?'} paid order silindi (biletler cascade)."
    if "TEARDOWN_NO_EVENT" in out:
        return False, "qa-test-etkinlik bulunamadı — önce QaTicketedEventSeeder çalıştırın."
    if "TEARDOWN_NO_USER" in out:
        return False, f"Youth test kullanıcısı yok: {youth_email}"
    if "TEARDOWN_SLUG_MISMATCH" in out:
        return False, "Güvenlik: slug eşleşmedi — teardown iptal (silme yapılmadı)."
    return False, ("Teardown başarısız (docker ayakta mı, container adı 'app' mı?). "
                   "Çıktı: " + out.strip()[:300])


# --- Teardown: sepeti temizle (tekrarlanabilirlik) -------------------------

def clear_cart(page, base_url):
    """Youth'un KENDİ sepetini, uygulamanın kendi 'Kaldır' route'uyla boşaltır
    (cart.destroy -> DELETE /sepet/{id}). Yalnızca giriş yapmış test kullanıcısının
    sepet kalemlerine dokunur; başka veriye dokunmaz. Kaldırılan kalem sayısını döner.

    Not: silme formu gerçek POST'tur ve JS confirm() sorar; diyalog çağıran tarafça
    (main'de page.on('dialog', accept)) otomatik kabul edilir.
    """
    page.goto(base_url + "/sepet", wait_until="load")
    page.wait_for_timeout(ALPINE_WAIT_MS)
    removed = 0
    while removed < 50:  # güvenlik: sonsuz döngü koruması
        btns = page.locator("[x-data^='cartItemCard'] form button:has(i.bi-x-lg)")
        if btns.count() == 0:
            break
        try:
            with page.expect_navigation(wait_until="load", timeout=NAV_TIMEOUT_MS):
                btns.first.click()
        except PlaywrightTimeoutError:
            break
        page.wait_for_timeout(ALPINE_WAIT_MS)
        removed += 1
    empty = page.locator("[x-data^='cartItemCard']").count() == 0
    return removed, empty


# --- Ana akış --------------------------------------------------------------

def run_flow(page, base_url, results, net_bad):
    """Plandaki 5 adım. results'a (ad, gecti, detay) ekler."""
    # ADIM 1 — Uygun etkinlik (dinamik seçim)
    step(1, "Satışta bilet olan bir etkinlik bul (dinamik)")
    event_url = find_suitable_event(page, base_url)
    if not event_url:
        failed(results, "etkinlik", "Satışta bilet içeren etkinlik bulunamadı "
                                    "(hiçbir adayda #bilet-al + '+' butonu yok).")
        return
    passed(results, "etkinlik", f"Seçilen etkinlik: {event_url}")

    panel = page.locator("#bilet-al")
    # Tekrarlayan etkinlikte ziyaret tarihi zorunlu olabilir -> min tarihi doldur.
    date_in = panel.locator("input[type=date]")
    if date_in.count() > 0:
        mn = date_in.first.get_attribute("min") or date.today().isoformat()
        date_in.first.fill(mn)
        print(f"   · tekrarlayan etkinlik: ziyaret tarihi '{mn}' olarak dolduruldu")

    # --- Buradan itibaren hata izleme akışa aittir: tamponları temizle ---
    net_bad.clear()

    # ADIM 2 — +1 bilet (etkinlik sayfasında stepper)
    step(2, "İlk bilet türünde adedi +1 yap (etkinlik sayfası)")
    plus = panel.locator("button:has(i.bi-plus)").first
    qty = qty_before_plus(plus)
    before = read_int(qty, 0)
    plus.click()
    page.wait_for_timeout(ALPINE_WAIT_MS)
    after = read_int(qty, before)
    add_btn = panel.get_by_role("button", name="Sepete Ekle")
    enabled = add_btn.is_enabled()
    if after == (before or 0) + 1 and enabled:
        passed(results, "bilet-ekle", f"adet {before}->{after}, 'Sepete Ekle' aktif")
    else:
        failed(results, "bilet-ekle",
                f"adet {before}->{after} (beklenen +1), 'Sepete Ekle' aktif={enabled}")
        if error_box_visible(panel):
            print(f"      ! panelde hata kutusu: {panel.locator('[x-text=errorMessage]').first.inner_text()}")
        return

    # ADIM 3 — "Sepete Ekle" -> /sepet'e in
    step(3, "'Sepete Ekle'ye tıkla ve /sepet'e yönlen")
    add_btn.click()
    navigated = True
    try:
        page.wait_for_url("**/sepet", timeout=NAV_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        navigated = False
    page.wait_for_timeout(ALPINE_WAIT_MS)
    if navigated and urlparse(page.url).path.rstrip("/") == "/sepet":
        passed(results, "sepete-ekle", f"POST sonrası /sepet'e inildi: {page.url}")
    else:
        detail = f"/sepet'e inilemedi (URL: {page.url})."
        if error_box_visible(panel):
            detail += " Panelde hata: " + panel.locator("[x-text=errorMessage]").first.inner_text()
        failed(results, "sepete-ekle", detail)
        return

    # ADIM 4 — Sepette kalem var mı?
    step(4, "Sepette en az bir kalem olduğunu doğrula")
    items = page.locator("[x-data^='cartItemCard']")
    n = items.count()
    if n >= 1:
        passed(results, "sepet-kalem", f"{n} sepet kalemi bulundu")
    else:
        failed(results, "sepet-kalem", "Sepette hiç kalem (cartItemCard) yok")
        return

    # ADIM 5 — Sepette adedi +1 yap
    step(5, "İlk sepet kaleminde katılımcı adedini +1 yap")
    item = items.first
    plus2 = item.locator("button:has(i.bi-plus)").first
    if plus2.count() == 0:
        failed(results, "sepet-artir", "Sepet kaleminde '+' (katılımcı) butonu yok "
                                       "(bilet dışı kalem olabilir).")
        return
    qty2 = qty_before_plus(plus2)
    before2 = read_int(qty2, 0)
    plus2.click()
    page.wait_for_timeout(AJAX_WAIT_MS)  # PATCH /sepet/{id}/yapilandir tamamlansın
    after2 = read_int(qty2, before2)
    if error_box_visible(item):
        failed(results, "sepet-artir",
                "Sepet kaleminde hata kutusu: " + item.locator("[x-text=error]").first.inner_text())
        return
    if after2 == (before2 or 0) + 1:
        passed(results, "sepet-artir", f"sepet adedi {before2}->{after2}")
    else:
        failed(results, "sepet-artir",
                f"adet {before2}->{after2} (beklenen +1). Bilet üst limiti (per_user_limit) "
                "1 ise bu limit olabilir — uygulama hatası olmayabilir; PATCH yanıtını kontrol edin.")


# --- Ödeme akışı (fake gateway) --------------------------------------------

def red_box_text(page):
    """Sayfada görünür kırmızı hata kutusu (bg-red-50 / text-red-600) varsa metnini
    döner. Tam-sayfa form POST akışında validation hatası böyle gösterilir."""
    boxes = page.locator("div.bg-red-50, .text-red-600")
    try:
        if boxes.count() > 0 and boxes.first.is_visible():
            return " | Kırmızı hata kutusu: " + boxes.first.inner_text().strip()[:200]
    except PlaywrightError:
        pass
    return ""


def run_payment_flow(page, base_url, results):
    """Sepet akışının DEVAMI: /sepet -> billing -> onayla -> ödemeye geç -> fake ödeme
    -> başarı. Tek-sipariş dalını (checkout.show) hedefler. Hata tespiti tam-sayfa form
    POST'a göre: kırmızı kutu + beklenmedik sayfada kalma (5xx global taramada yakalanır).
    """
    # ADIM A — sepette fatura bilgileri + "Sepeti onayla"
    step("A", "Fatura bilgilerini doldur ve 'Sepeti onayla' (/sepet)")
    page.goto(base_url + "/sepet", wait_until="load")
    page.wait_for_timeout(ALPINE_WAIT_MS)

    # Ödenebilirlik: her katılımcı ad-soyad ister; yalnızca 1. katılımcı profilden
    # otomatik dolar. Sepet akışı adedi 2 yaptığından, ödeme için TEK katılımcıya indir
    # (2. katılımcı bilgisi olmadan "Sepeti onayla" pasif kalır). Buton pasifliği
    # sunucu-tarafı render olduğundan, adedi düşürünce /sepet'i yeniden yükle.
    item = page.locator("[x-data^='cartItemCard']").first
    if item.count() > 0:
        qspan = item.locator("[x-text='getQty()']").first
        dash = item.locator("button:has(i.bi-dash)").first
        reduced = 0
        while reduced < 10 and read_int(qspan, 1) > 1 and dash.count() > 0:
            dash.click()
            page.wait_for_timeout(AJAX_WAIT_MS)  # PATCH /yapilandir
            reduced += 1
        if reduced > 0:
            print(f"   · ödenebilirlik: katılımcı adedi 1'e indirildi ({reduced} adım), /sepet yeniden yükleniyor")
            page.goto(base_url + "/sepet", wait_until="load")
            page.wait_for_timeout(ALPINE_WAIT_MS)

    form = page.locator("form[action*='/sepet/odeme']")
    if form.count() == 0:
        failed(results, "odeme-onayla", "/sepet'te checkout formu (cart.checkout) yok — sepet boş olabilir.")
        return
    form.locator("input[name='billing_title']").fill("QA Test Fatura")
    form.locator("textarea[name='billing_address']").fill("QA Test Mah. Test Sok. No:1")
    form.locator("input[name='billing_city']").fill("İstanbul")
    form.locator("input[name='billing_district']").fill("Kadıköy")
    onayla = form.get_by_role("button", name="Sepeti onayla")
    if not onayla.is_enabled():
        failed(results, "odeme-onayla", "'Sepeti onayla' pasif (eksik katılımcı bilgisi olabilir).")
        return
    try:
        with page.expect_navigation(wait_until="load", timeout=NAV_TIMEOUT_MS):
            onayla.click()
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(ALPINE_WAIT_MS)
    if urlparse(page.url).path.rstrip("/") == "/sepet/kontrol":
        passed(results, "odeme-onayla", "fatura kaydedildi, /sepet/kontrol'e geçildi")
    else:
        failed(results, "odeme-onayla", f"/sepet/kontrol'e geçilemedi (URL: {page.url})." + red_box_text(page))
        return

    # ADIM B — özet: onay kutusu + "Ödemeye Geç"
    step("B", "Sipariş özetinde onay kutusu + 'Ödemeye Geç' (/sepet/kontrol)")
    page.locator("input[name='confirm']").check()
    try:
        with page.expect_navigation(wait_until="load", timeout=NAV_TIMEOUT_MS):
            page.get_by_role("button", name="Ödemeye Geç").click()
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(ALPINE_WAIT_MS)
    if re.match(r"^/checkout/\d+", urlparse(page.url).path):
        passed(results, "odemeye-gec", f"sipariş oluştu: {page.url}")
    else:
        detail = f"/checkout/{{order}}'a geçilemedi (URL: {page.url})."
        if "/sepet/odeme/" in page.url:
            detail += " (çoklu-sipariş dalı — bu akış tek-sipariş hedefliyor)"
        failed(results, "odemeye-gec", detail + red_box_text(page))
        return

    # ADIM C — ödeme sayfası: "Ödemeyi Tamamla"
    step("C", "Ödeme sayfasında 'Ödemeyi Tamamla' (/checkout/{order})")
    pay = page.locator("button[formaction*='/pay']")
    if pay.count() == 0:
        failed(results, "odemeyi-tamamla", "'Ödemeyi Tamamla' butonu (formaction=checkout.pay) yok.")
        return
    try:
        with page.expect_navigation(wait_until="load", timeout=NAV_TIMEOUT_MS):
            pay.first.click()
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(ALPINE_WAIT_MS)
    if re.match(r"^/fake-pay/\d+", urlparse(page.url).path):
        passed(results, "odemeyi-tamamla", f"fake ödeme sayfasına yönlendirildi: {page.url}")
    else:
        failed(results, "odemeyi-tamamla", f"/fake-pay/{{order}}'a geçilemedi (URL: {page.url})." + red_box_text(page))
        return

    # ADIM D — sahte ödeme: "Başarılı Ödeme (Simülasyon)" (.btn-success)
    step("D", "Sahte ödeme sayfasında 'Başarılı Ödeme (Simülasyon)' (/fake-pay/{order})")
    succ = page.locator("form[action*='/fake-pay/'][action$='/success'] button[type='submit']")
    if succ.count() == 0:
        failed(results, "fake-basarili", "'Başarılı Ödeme' butonu bulunamadı (success formu yok).")
        return
    try:
        with page.expect_navigation(wait_until="load", timeout=NAV_TIMEOUT_MS):
            succ.first.click()
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(ALPINE_WAIT_MS)
    on_dashboard = urlparse(page.url).path.rstrip("/") == "/dashboard"
    success_flash = page.get_by_text("Ödemeniz başarılı", exact=False).count() > 0
    if on_dashboard and success_flash:
        passed(results, "fake-basarili", "ödeme başarılı — /dashboard + 'Ödemeniz başarılı' mesajı")
    else:
        failed(results, "fake-basarili",
               f"başarı doğrulanamadı (URL: {page.url}, başarı mesajı={success_flash})." + red_box_text(page))


# --- Orkestrasyon ----------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="badinext ilk E2E akışı — bilet ekle + sepette adet artır (güvenli-varsayılan)")
    parser.add_argument("--url", default="http://localhost:8080", help="Base URL")
    parser.add_argument("--role", choices=["youth"], default="youth",
                        help="Login rolü (ilk akış: yalnızca youth)")
    parser.add_argument("--target-env", default=None,
                        help="Hedef Laravel src/.env yolu (güvenlik kontrolü için). "
                             "Verilmezse TARGET_REPO_PATH/src/.env kullanılır.")
    parser.add_argument("--headed", action=argparse.BooleanOptionalAction, default=True,
                        help="Tarayıcıyı görünür çalıştır (varsayılan). CI için --no-headed.")
    parser.add_argument("--slow-mo", type=int, default=350,
                        help="Headed modda aksiyonlar arası yavaşlatma (ms), izlenebilir olsun diye")
    parser.add_argument("--gates-only", action="store_true",
                        help="Yalnızca kapıları (güvenlik + login + profil) çalıştır; "
                             "akışa GİRME. Profil ön koşulunu doğrulamak için.")
    parser.add_argument("--pay", action="store_true",
                        help="Sepet akışının ardından ÖDEME akışını da çalıştır (fake gateway). "
                             "Başında ödenmiş test siparişlerini scoped teardown ile temizler.")
    args = parser.parse_args()

    base_url = args.url.rstrip("/")
    base_host = urlparse(base_url).netloc

    # === GÜVENLİK ÖN KOŞULU (her çalıştırmanın ilk işi) ===
    hdr("GÜVENLİK ÖN KOŞULU")
    target_env = resolve_target_env(args.target_env)
    ok, lines = check_security_precondition(target_env)
    for ln in lines:
        print("  " + ln)
    if not ok:
        print("\n[REDDEDİLDİ] Güvenli ortam doğrulanamadı — hiçbir aksiyon alınmadı.\n")
        sys.exit(2)
    print("\n  -> Ortam güvenli. Devam ediliyor.")

    # Youth kimlik bilgileri (runner ile aynı .env kaynağı ve fallback)
    load_dotenv(ROOT / ".env")
    email = (os.environ.get("QA_YOUTH_EMAIL", "").strip()
             or os.environ.get("QA_LOGIN_EMAIL", "").strip()
             or "test-youth@qa.local")
    password = os.environ.get("QA_YOUTH_PASSWORD", "") or os.environ.get("QA_LOGIN_PASSWORD", "")
    if not password:
        print("\n[HATA] youth şifresi yok: web-qa-agent/.env'e QA_YOUTH_PASSWORD "
              "(veya QA_LOGIN_PASSWORD) ekleyin.\n")
        sys.exit(1)

    results = []
    net_bad = []  # akışa ait 4xx/5xx yanıtlar

    with sync_playwright() as p:
        # Erişilebilirlik: hedef ayakta mı?
        req = p.request.new_context(ignore_https_errors=True)
        try:
            reachable = req.get(base_url, timeout=10_000).status < 600
        except PlaywrightError:
            reachable = False
        req.dispose()
        if not reachable:
            print(f"\n[HATA] Hedef ayakta değil: {base_url} — önce 'docker compose up'.\n")
            sys.exit(1)

        browser = p.chromium.launch(headless=not args.headed,
                                    slow_mo=args.slow_mo if args.headed else 0)

        # === LOGIN (runner.do_login yeniden kullanılır) ===
        hdr("LOGIN")
        print(f"  Giriş deneniyor (rol=youth): {email}")
        success, auth_state, final_url = do_login(browser, base_url, email, password, "/login")
        if not success:
            print(f"\n[HATA] Login başarısız — hesap active mi, şifre doğru mu? (URL: {final_url})\n")
            browser.close()
            sys.exit(1)
        print(f"  ✓ Login başarılı. (URL: {final_url})")

        # Tüm akış tek, kimlikli, görünür context'te sürülür.
        context = browser.new_context(ignore_https_errors=True, storage_state=auth_state)

        # === PROFİL ÖN KOŞULU ===
        hdr("PROFİL ÖN KOŞULU")
        reachable_flow, purl, reason = check_profile_precondition(context, base_url)
        if not reachable_flow:
            print(f"  ✗ Hesap sepet akışına ULAŞAMIYOR — profile yönlendirildi: {purl}\n")
            for ln in profile_remediation(reason):
                print("  " + ln)
            print("\n[DURDURULDU] Profil ön koşulu sağlanmadı. Akış çalıştırılmadı.\n")
            context.close()
            browser.close()
            sys.exit(3)
        print(f"  ✓ Hesap korumalı sepet sayfasına ulaşabiliyor ({purl}). Devam.")

        if args.gates_only:
            hdr("SADECE KAPILAR (--gates-only)")
            print("  ✓ Güvenlik + Login + Profil kapıları GEÇTİ. Akışa girilmedi.")
            print("  Tam akış için --gates-only olmadan çalıştırın.\n")
            context.close()
            browser.close()
            sys.exit(0)

        # === AKIŞ ===
        hdr("E2E AKIŞI: bilet ekle -> sepette adet artır")
        page = context.new_page()
        page.set_default_navigation_timeout(NAV_TIMEOUT_MS)

        js_errors = []
        page.on("console", lambda m: js_errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: js_errors.append(f"pageerror: {e}"))
        # Silme formu confirm() sorar -> teardown'ın otomatik kabul etmesi için.
        page.on("dialog", lambda d: d.accept())

        def on_response(r):
            try:
                if r.status >= 400 and (urlparse(r.url).netloc == base_host):
                    # Akışın kritik uçları veya aynı-host 5xx -> gerçek hata
                    if any(k in r.url for k in (
                        "/sepete-ekle", "/odeme/dogrula", "/yapilandir",
                        "/sepet/odeme", "/sepet/onayla", "/checkout/", "/fake-pay/")) \
                            or r.status >= 500:
                        net_bad.append((r.url, r.status))
            except PlaywrightError:
                pass
        page.on("response", on_response)

        # TEARDOWN (ödeme) — YALNIZCA --pay: youth + qa-test-etkinlik paid order'larını
        # scoped tinker ile sil (biletler cascade). Başarısızsa körlemesine devam etme.
        if args.pay:
            print("\n[TEARDOWN-ÖDEME] Ödenmiş test siparişleri temizleniyor (scoped: youth + qa-test-etkinlik)...")
            target_repo = resolve_target_repo()
            if not target_repo or not Path(target_repo).is_dir():
                print(f"   ! TARGET_REPO_PATH geçersiz ({target_repo}) — teardown yapılamıyor.")
                context.close()
                browser.close()
                sys.exit(4)
            tok, tmsg = teardown_paid_orders(target_repo, email)
            print(f"   · {tmsg}")
            if not tok:
                print("\n[DURDURULDU] Ödeme teardown başarısız — körlemesine devam edilmiyor.\n")
                context.close()
                browser.close()
                sys.exit(4)

        # TEARDOWN — tekrarlanabilirlik: akış temiz sepetle başlasın.
        print("\n[TEARDOWN] Youth'un sepeti temizleniyor (temiz başlangıç)...")
        try:
            removed, empty = clear_cart(page, base_url)
            state = "temiz" if empty else "HÂLÂ DOLU"
            print(f"   · {removed} kalem kaldırıldı — sepet: {state}")
            if not empty:
                print("   ! Uyarı: sepet tam boşalmadı; akış yine de denenecek.")
        except PlaywrightError as e:
            print(f"   ! Teardown hatası (yok sayılıp devam): {e}")

        try:
            run_flow(page, base_url, results, net_bad)
            # Ödeme akışı SADECE sepet akışı sorunsuzsa ve --pay verildiyse.
            if args.pay and all(ok for _, ok, _ in results):
                hdr("ÖDEME AKIŞI: sepet -> fake gateway ile öde")
                run_payment_flow(page, base_url, results)
        except PlaywrightError as e:
            failed(results, "akış", f"Beklenmeyen Playwright hatası: {e}")

        # Genel hata taraması (happy-path: hepsi FAIL sebebi)
        step(6, "Genel hata taraması (console / pageerror / HTTP)")
        clean = True
        if js_errors:
            clean = False
            print(f"   ✗ {len(js_errors)} JS hatası:")
            for e in js_errors[:10]:
                print(f"      - {e}")
        if net_bad:
            clean = False
            print(f"   ✗ {len(net_bad)} hatalı HTTP yanıtı (4xx/5xx):")
            for u, s in net_bad[:10]:
                print(f"      - [{s}] {u}")
        if clean:
            passed(results, "hata-taramasi", "console/pageerror yok, 4xx/5xx yok")
        else:
            failed(results, "hata-taramasi", "console/HTTP hatası tespit edildi (yukarıda)")

        context.close()
        browser.close()

    # === ÖZET ===
    hdr("ÖZET")
    all_ok = all(ok for _, ok, _ in results) and len(results) > 0
    for name, ok, detail in results:
        mark = "✓" if ok else "✗"
        print(f"  {mark} {name:<16} {detail}")
    print("-" * 62)
    if all_ok:
        print("  SONUÇ: ✓ AKIŞ BAŞARILI — bilet eklendi ve sepette adet artırıldı, hata yok.")
    else:
        first_fail = next((n for n, ok, _ in results if not ok), "?")
        print(f"  SONUÇ: ✗ AKIŞ BAŞARISIZ — ilk kalınan adım: '{first_fail}'.")
    print("=" * 62 + "\n")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
