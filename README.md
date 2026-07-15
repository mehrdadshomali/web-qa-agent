# web-qa-agent

![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)
![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)
![Tested with Playwright](https://img.shields.io/badge/E2E-Playwright-2EAD33.svg)
![AI](https://img.shields.io/badge/AI-Anthropic%20Claude-D97757.svg)

**AI destekli QA + E2E test ajanı** — herhangi bir web uygulamasına tarayıcı
üzerinden bağlanır. İki tamamlayıcı yeteneği vardır:

1. **Salt-okunur QA gezgini + AI rapor üreticisi** — guest ve (opsiyonel) giriş
   yapılmış yüzeyi gezer; teknik/erişilebilirlik/performans verisi toplar ve
   Anthropic Claude ile önceliklendirilmiş bir kalite raporu üretir. İsteğe bağlı
   olarak sonucu Telegram'a gönderir ve haftalık otomatik çalışır.
2. **Uçtan uca (E2E) akış testleri** — Playwright ile kritik kullanıcı yolculuklarını
   *gerçekten* sürer (tıklar, form doldurur, satın alır). Bu kısım **aktif** aksiyon
   alır ama katı bir **güvenli-ortam kapısının** arkasındadır: yalnızca mailler log'a
   yazılan ve ödemeler sahte (fake) sağlayıcıya giden **yerel/izole** bir ortamda
   çalışır (aşağıya bakın).

**Salt-okunur gezgin (1. yetenek):** yalnızca `GET` navigasyonu. Hiçbir form
gönderilmez (yalnızca envantere alınır); durum değiştiren uçlar (logout, delete,
checkout, sepet/ödeme, `/admin`, vb. — İngilizce ve Türkçe kalıplar) atlanır. Tek
istisna, açıkça istendiğinde `/login` formunun doldurulup gönderilmesidir (auth
arkasını gezmek için).

**E2E akışları (2. yetenek):** durum değiştiren gerçek aksiyonlar (bkz.
[E2E akış testleri](#e2e-akış-testleri)). Her çalıştırma, hedefin `.env`'ini
okuyup `MAIL_MAILER=log` ve ödeme sağlayıcısının `fake`/boş olduğunu doğrular;
aksi hâlde çalışmayı reddeder — böylece yanlışlıkla gerçek mail/ödeme moduna
geçmiş bir ortamda hiçbir aksiyon alınmaz.

Server-side render eden uygulamalar için tasarlanmıştır (ör. Blade + Alpine.js
gibi yapılar; ağır animasyon kütüphaneleri — Three.js/GSAP — göz önünde
bulundurulmuştur). Salt-okunur gezgin hedefin kaynak koduna/veritabanına erişmez;
E2E akışları ise yalnızca tekrarlanabilirlik için (teardown) hedefin *yerel* test
veritabanına dokunur — sıkı, kapsamı sınırlı biçimde.

---

## Mimari / akış

```
# Salt-okunur QA gezgini + AI rapor
runner.py     →  reports/findings.json      (salt-okunur tarama: teknik + a11y + perf)
analyze.py    →  reports/report.md          (findings'i damıtıp Claude'a gönderir)
run_qa.py     →  yukarıdaki ikisini tek komutta zincirler
telegram_bot.py →  botla /tara, /test komutları; raporu Telegram'a gönderir
weekly_run.py + launchd  →  haftalık otomatik çalışma + Telegram teslimi

# Aktif E2E akış testleri (güvenli-ortam kapısı arkasında)
flow_tests/ticket_cart_flow.py  →  sepet / ödeme / kayıt+mail E2E akışları (headed)
```

Salt-okunur katmanlar incedir ve bir alttakini çağırır; alt bileşenlere dokunmaz.
E2E modülü ayrıdır: gezginin `do_login` ve dinleyici mantığını **yeniden kullanır**
ama onu değiştirmez.

---

## Gereksinimler

- Python 3.11+
- Bir Anthropic API anahtarı (AI raporu için) — https://console.anthropic.com
- (Opsiyonel) Bir Telegram botu — @BotFather
- Taranacak, çalışır durumda bir hedef web uygulaması (varsayılan
  `http://localhost:8080`)

## Kurulum

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# Yapılandırma: örnek dosyadan kendi .env'inizi oluşturun ve değerleri doldurun
cp .env.example .env
# .env dosyasını açıp ANTHROPIC_API_KEY vb. değerleri girin
```

`.env` git'e dahil **edilmez** (`.gitignore`). Anahtarlarınızı asla commit'lemeyin.

### `.env` değişkenleri

| Değişken | Ne için | Gerekli mi |
|---|---|---|
| `ANTHROPIC_API_KEY` | AI raporu (`analyze.py`) | AI raporu için evet |
| `QA_LOGIN_EMAIL` / `QA_LOGIN_PASSWORD` | `--login` ile auth arkasını gezmek | Login taraması için |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Telegram teslimi | Telegram/haftalık için |
| `QA_URL` | Varsayılan hedef URL (opsiyonel) | Hayır |

> Login için, hedef uygulamada **atılabilir bir test hesabı** kullanın (gerçek
> kullanıcı değil). Bu araç yalnızca guest + o test rolünün *görebildiği* yüzeyi
> salt-okunur gezmek içindir.

---

## Kullanım

### 1) Tarama — `runner.py`

```bash
python runner.py                                   # varsayılan: --url http://localhost:8080 --max-pages 50
python runner.py --url http://localhost:8080 --max-pages 200
python runner.py --login --max-pages 200           # auth arkasını da gez (.env kimlik bilgileriyle)
```

Hedefe erişilemiyorsa net bir mesaj basıp temiz çıkar. Her benzersiz sayfa için
toplananlar:

- **HTTP durum kodu** (2xx olmayanlar işaretli); 500'lerde çerçeve istisna
  türü/mesajı (ör. Laravel, `APP_DEBUG=true` varsayımıyla).
- **Konsol mesajları** (error/warning öncelikli).
- **Başarısız ağ istekleri**, host'a göre kategorize: `site_resource` (gerçek
  kırık site kaynağı), `third_party` (dış: analytics vb.), `vite_hmr` (dev-server).
- **Kırık link/görsel** (`<a>`/`<img>` hedefleri hafif HEAD/GET ile); global
  `broken_targets` özetinde benzersizleştirilir.
- **Form envanteri** (tespit, **gönderme yok**): adet, action, method, CSRF
  alanı, zorunlu input'lar.
- **Erişilebilirlik**: vendor'lanmış axe-core ile WCAG 2.0/2.1 A+AA ihlalleri.
- **Performans** (hafif, Lighthouse'suz): yükleme süresi, DOMContentLoaded,
  transfer edilen byte, istek sayısı.
- Tam sayfa **ekran görüntüsü**.

**Verimlilik:** URL'ler path bazında benzersizleştirilir (query varyantları tek
sayfa sayılır); indirilebilir dosyalar (PDF vb.) ayrı kategoride tutulur.

### 2) AI raporu — `analyze.py`

```bash
python analyze.py
```

`findings.json`'u **damıtır** (ham node listeleri/ekran görüntüleri gönderilmez;
yalnızca kompakt özet), `claude-sonnet-4-6`'ya gönderir ve
`reports/report.md`'ye önceliklendirilmiş (Kritik/Orta/Düşük) bir rapor yazar.
Rapor, **ölçülen tespitleri** (kesin) **kök neden çıkarımlarından** (dış gözleme
dayalı, doğrulanmalı) ayırır. API başarısız olursa yapay zeka olmadan yerel bir
rapor üretir; her çalışmada token kullanımı + kaba maliyet basılır.

### 3) Tek komut zinciri — `run_qa.py`

```bash
python run_qa.py                    # tam tarama (login, 200 sayfa) + AI raporu
python run_qa.py --max-pages 50
python run_qa.py --no-login         # yalnızca guest
```

Tarama başarısız olursa (hedef kapalı / login başarısız) AI adımına **geçmez**
— yarım veriyle API'ye gidip masraf yapılmaz. Hem CLI'dan hem programatik
(`run_qa()`) çağrılabilir.

### 4) Telegram botu — `telegram_bot.py`

```bash
python telegram_bot.py
```

Yalnızca `.env`'deki `TELEGRAM_CHAT_ID`'ye cevap verir (başka herkesi yok sayar).
Komutlar: `/start` (yardım), `/tara` (tam tarama; `/tara 50` ile sayfa override),
`/test` (hızlı 5 sayfa). Bitince özet + `report.md` dosyasını gönderir.
`chat_id`'nizi öğrenmek için: bota bir mesaj yazın, sonra `python get_chat_id.py`.

### 5) Haftalık otomatik çalışma (macOS launchd) — `weekly_run.py`

`weekly_run.py`, `run_qa()` ile tam taramayı yapıp sonucu Telegram'a gönderen
tek-seferlik bir scripttir. `deploy/` altındaki örnek `launchd` plist'i ile
haftada bir tetiklenir.

```bash
# Örnek plist'i kopyalayın ve içindeki YOLLARI kendi kurulumunuza göre düzenleyin
cp deploy/com.example.qa-weekly.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.example.qa-weekly.plist
launchctl list | grep qa-weekly

# Elle test (zamanı beklemeden bir kez):
launchctl start com.example.qa-weekly
#   veya doğrudan (launchd olmadan):
venv/bin/python weekly_run.py

# Kaldırma:
launchctl unload -w ~/Library/LaunchAgents/com.example.qa-weekly.plist
```

Çalışma geçmişi `logs/weekly.log`'a; launchd çıktısı `logs/launchd.*.log`'a yazılır.

---

## E2E akış testleri

`flow_tests/ticket_cart_flow.py`, kritik kullanıcı yolculuklarını Playwright ile
**gerçekten sürer** (headed/görünür varsayılan). Salt-okunur gezginden farklı olarak
bu akışlar **durum değiştirir** (tıklama, form, satın alma); bu yüzden her çalıştırma
katı bir güvenli-ortam kapısıyla başlar.

### Güvenlik kapısı (her çalıştırmadan önce)

Akış, hedef uygulamanın `src/.env`'ini okur ve **yalnızca** şu koşullarda devam eder:

- `MAIL_MAILER=log` → mailler dışarı çıkmaz, yalnızca hedefin loguna yazılır.
- Ödeme sağlayıcısı `fake` veya boş → gerçek ödeme sağlayıcısına (gerçek para) gidilmez.

Koşullar sağlanmazsa ya da `src/.env` bulunamazsa, çalışma **reddedilir** (hiçbir
aksiyon alınmaz). Hedefin yolu `web-qa-agent/.env` içindeki `TARGET_REPO_PATH`'ten
çözülür veya `--target-env` ile verilir.

### Ön koşullar

- Çalışır durumda **yerel/izole** bir hedef uygulama (`docker compose` ile ayakta).
- Hedefte seed'lenmiş **atılabilir test verisi**: bir test kullanıcısı (profil tam +
  e-posta doğrulanmış) ve satışta ürünü olan bir test kaydı. (Bu araç hedefin
  koduna dosya eklemez; test verisini hedefin kendi seed mekanizmasıyla hazırlarsınız.)

### Üç akış

```bash
# 1) Sepet akışı (varsayılan): ürün ekle → sepette adedi artır → doğrula
python flow_tests/ticket_cart_flow.py

# 2) Ödeme akışı: sepet akışının devamı → fatura → onay → fake gateway ile öde → doğrula
python flow_tests/ticket_cart_flow.py --pay

# 3) Kayıt + doğrulama maili: yeni (throwaway) kullanıcı kaydı → doğrulama maili log'da mı
python flow_tests/ticket_cart_flow.py --register-mail

# Yalnızca kapıları (güvenlik + login + profil) çalıştır, akışa girme:
python flow_tests/ticket_cart_flow.py --gates-only

# Görünür yerine başsız (CI):
python flow_tests/ticket_cart_flow.py --no-headed
```

| Akış | Ne yapar | "Başarılı" tanımı |
|---|---|---|
| **Sepet** (varsayılan) | Giriş → uygun ürünü **dinamik** seçer → sepete ekler → sepette adedi artırır | Her adım DOM'da doğrulanır; console/pageerror/4xx/5xx yok |
| **Ödeme** (`--pay`) | Sepet akışının devamı → fatura bilgileri → onay → **fake** ödeme sağlayıcısıyla tamamlar | Başarı sayfasına yönlenir + görünür başarı mesajı; gerçek para yok |
| **Kayıt+mail** (`--register-mail`) | Guest → yeni kullanıcı kaydı → doğrulama maili tetiklenir | Kayıt tamamlanır **ve** doğrulama maili hedefin loguna yazılır (dışarı çıkmadan) |

Selector'lar hedefin **mevcut yapısal** öğelerinden türetilir (form `action`/`name`,
buton metni, kararlı `id`/attribute); hedefin şablonlarına test-özel işaretleyici
(`data-testid`) **eklenmez**.

### Tekrarlanabilirlik (teardown)

Her akış temiz başlar ve kalıcı test artığı bırakmaz:

- **Sepet/ödeme:** akış başında test kullanıcısının sepeti, uygulamanın kendi "kaldır"
  akışıyla temizlenir; ödeme akışında ayrıca test kullanıcısının **yalnızca test
  kaydına** ait ödenmiş siparişler, **çift koşullu** (kullanıcı + kayıt kimliği) scoped
  bir temizlikle silinir (bağlı kayıtlar FK cascade ile gider).
- **Kayıt+mail:** her koşu benzersiz bir `qa-mailtest-{zaman}@qa.local` e-postası üretir;
  koşu sonunda o kullanıcı **üç kat guard** (prefix + domain + tam eşleşme) ile silinir —
  gerçek veya başka bir kullanıcıya dokunmak imkânsızdır.

Teardown, hedefin yerel test veritabanında `docker compose exec` ile çalışır; başarısız
olursa (ör. konteyner ayakta değil) akış körlemesine devam etmez, net bir mesajla durur.

**⚠️ Mac uyku / kapalı durumu:**
- **Uyanık:** job zamanında çalışır.
- **Uykuda:** launchd kaçırılan çalışmayı Mac uyanınca **bir kez** çalıştırır
  (varsayılan; ekstra ayar gerekmez).
- **Tamamen kapalı:** çalışma **garanti değildir** — o saatte Mac'in açık (uyanık
  ya da uykuda) olması gerekir. İsteğe bağlı olarak Mac'i job'dan önce uyandırmak
  için: `sudo pmset repeat wakeorpoweron M 08:55:00`. Kaçırılmaması kritikse
  sürekli açık bir sunucu/CI (cron) daha uygundur.

---

## Çıktılar

- `reports/findings.json` — sayfa bazlı ham bulgular + global özetler.
- `reports/report.md` — AI tarafından üretilen önceliklendirilmiş rapor.
- `reports/screenshots/` — sayfa ekran görüntüleri.
- `logs/weekly.log` — haftalık çalışma geçmişi.

> `reports/` ve `logs/` git'e dahil **edilmez** — tarama bulguları (hedefe özel
> olabilir) repoya girmez.

---

## Gizlilik ve güvenlik

- Tüm sırlar `.env`'de tutulur; `.env` gitignore'dadır ve API anahtarı/token
  hiçbir zaman loglanmaz.
- Tarama bulguları (`reports/`) ve loglar (`logs/`) versiyon kontrolüne girmez.
- **Salt-okunur gezgin** hedef üzerinde durum değiştiren hiçbir işlem yapmaz
  (yalnızca `GET` + istenirse `/login`).
- **E2E akışları aktif aksiyon alır**, ama yalnızca güvenli-ortam kapısı geçtiğinde
  (mail=log, ödeme=fake) çalışır; yalnızca yerel/izole bir hedefte kullanın. Mailler
  dışarı çıkmaz, ödemeler sahtedir, teardown kapsamı test verisiyle sınırlıdır.

## Lisans

MIT — bkz. [LICENSE](LICENSE).
