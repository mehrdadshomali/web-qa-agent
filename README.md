# web-qa-agent

Bağımsız bir **salt-okunur QA gezgini (crawler)**. Hedef bir web uygulamasına
yalnızca ağ üzerinden (HTTP) bağlanır, gezer ve veri toplar. Hiçbir form
göndermez, durum değiştiren hiçbir işlem yapmaz, giriş yapmaz ve dış servise
(yapay zeka vb.) istek atmaz.

Hedef, Docker ile çalışan bir Laravel 12 uygulamasıdır (varsayılan
`http://localhost:8080`). Bu proje hedeften tamamen ayrıdır; hedefin dosyalarını
okumaz, sadece HTTP üzerinden erişir.

## Kurulum

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

Bağımlılıklar: `playwright`, `beautifulsoup4`.

## Çalıştırma

Önce hedef uygulamanın ayakta olduğundan emin olun (badinext tarafında
`docker compose up`). Sonra:

```bash
python runner.py                                  # varsayılan: --url http://localhost:8080 --max-pages 50
python runner.py --url http://localhost:8080 --max-pages 200
```

Erişilemiyorsa araç net bir mesaj basıp temiz çıkar.

## Faz 1: ne yapar

Guest (giriş yapılmamış) yüzeyini salt-okunur gezer ve her benzersiz sayfa için
veri toplar:

- **Gezme:** Yalnızca GET navigasyonu. Sayfa yükleme `load` event'ini bekler,
  ardından Alpine.js init için kısa sabit bir bekleme uygular (`networkidle`
  kullanmaz — Three.js/animasyon döngüsü yüzünden ağ sakinleşmeyebilir).
- **Salt-okunur kapsam:** `<a href>` linklerini takip eder, sadece aynı host'taki
  iç linkleri gezer. Durum değiştiren / auth / ödeme kalıplarını (`logout`,
  `delete`, `checkout`, `cart`, `/admin`, vb.) atlar. Login'e yönlendiren
  sayfaları "auth-required" olarak işaretler, giriş yapmaz.
- **URL normalizasyonu:** Query string ve fragment atılarak path bazında
  benzersizleştirir (`?sort=`/`?category=`/`?layout=` varyantları tek sayfa
  sayılır; görülen varyantlar yine `query_variants_seen` olarak kaydedilir).
- **İndirilebilir dosyalar:** PDF vb. indirme uçları ayrı bir `downloadable_files`
  kategorisinde tutulur (HTTP durumu hafif bir istekle alınır), "yüklenemeyen
  sayfa" sayılmaz.

Her sayfa için toplananlar:

- HTTP durum kodu (2xx olmayanlar işaretli); 500 veren sayfalarda Laravel
  istisna türü/mesajı (APP_DEBUG=true varsayımıyla).
- Tüm konsol mesajları (error/warning öncelikli).
- Başarısız ağ istekleri, host'a göre kategorize: `site_resource` (gerçek kırık
  site kaynağı), `third_party` (dış: GA/GTM vb.), `vite_hmr` (localhost:5173).
- `<a>` ve `<img>` hedeflerinin durumu (hafif HEAD/GET); kırık link/görseller.
  Kırık hedefler ayrıca global `broken_targets` özetinde benzersizleştirilir
  (URL + status + referans sayısı + hangi sayfalarda göründüğü).
- Form envanteri (tespit, **gönderme yok**): adet, action, method, CSRF alanı,
  zorunlu input'lar.
- Tam sayfa ekran görüntüsü.

## Çıktı

- `reports/findings.json` — sayfa bazlı tüm bulgular + global `broken_targets`,
  `downloadable_files` özetleri.
- `reports/screenshots/` — sayfa ekran görüntüleri.
- Terminale kısa özet (gezilen path, 2xx olmayan, konsol hatası, kırık
  hedef/kaynak, indirilebilir dosya, auth-atlanan, form sayısı).

> Bulgular `reports/findings.json` içindedir; bu README aracı anlatır, bulguları
> içermez. `reports/` ve `venv/` git'e dahil edilmez.

## Faz 4: Haftalık otomatik çalışma (launchd)

`weekly_run.py`, `run_qa()` ile tam taramayı (login, max 200) yapıp sonucu
Telegram'a gönderen tek-seferlik bir scripttir. macOS `launchd` ile haftada bir
tetiklenir.

### Kurulum

```bash
# 1) plist'i LaunchAgents'a kopyala (yollar makineye özgü — plist içindekilerle eşleşmeli)
cp deploy/com.badinext.qa-weekly.plist ~/Library/LaunchAgents/

# 2) yükle (her Pazartesi 09:00'da çalışacak şekilde kaydeder)
launchctl load -w ~/Library/LaunchAgents/com.badinext.qa-weekly.plist

# durum:
launchctl list | grep qa-weekly
```

### Elle test (kurulumu doğrulamak için hemen bir kez çalıştır)

```bash
# launchd üzerinden tetikle:
launchctl start com.badinext.qa-weekly
#   veya modern söz dizimi:
launchctl kickstart -k gui/$(id -u)/com.badinext.qa-weekly

# ya da doğrudan (launchd olmadan):
venv/bin/python weekly_run.py
```

Sonuç Telegram'a düşer (özet + `report.md`). Çalışma geçmişi:
`logs/weekly.log`; launchd job çıktısı: `logs/launchd.out.log` / `logs/launchd.err.log`.

### Kaldırma

```bash
launchctl unload -w ~/Library/LaunchAgents/com.badinext.qa-weekly.plist
```

### ⚠️ Mac uyku / kapalı durumu (önemli)

- **Mac uyanık:** job zamanında (Pzt 09:00) çalışır.
- **Mac uykuda:** `launchd`, `StartCalendarInterval` için kaçırılan çalışmayı Mac
  **uyanınca bir kez** çalıştırır (varsayılan davranış; ekstra ayar gerekmez).
- **Mac tamamen KAPALI:** çalışma **garanti değildir**. Bu yüzden **o saatte Mac'in
  açık (uyanık ya da uykuda) olması gerekir** — kapalıysa o haftalık tarama atlanabilir.

İsteğe bağlı: Mac'i her Pazartesi 08:55'te otomatik uyandırmak (sonra launchd 09:00'da
tetikler) için:

```bash
sudo pmset repeat wakeorpoweron M 08:55:00
```

Bu, "her koşulda çalışır" garantisi vermez ama uyku senaryosunu güçlendirir.
Kritik bir ortamda haftalık taramanın kaçmaması gerekiyorsa, sürekli açık bir
sunucu/CI (cron) daha uygundur.

## Kapsam dışı

Şimdilik: brand/student_club/community rol taramaları ve tam Lighthouse skorları
(performans hâlâ hafif metriklerle) sonraki fazlara bırakılmıştır.
