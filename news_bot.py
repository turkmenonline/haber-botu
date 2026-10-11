#!/usr/bin/env python3
"""Araba + F1 haber botu.

RSS kaynakları -> Gemini (Türkçe özet + önem puanı) -> ntfy bildirimi.

Kullanım:
    python news_bot.py check    # yeni haberleri tara, önemli olanları anında gönder
    python news_bot.py digest   # 24 saatlik özeti gönder
"""
import calendar
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import time
from zoneinfo import ZoneInfo

import feedparser
import requests

TZ = ZoneInfo("Europe/Istanbul")
DATA_DIR = "data"
SEEN_FILE = os.path.join(DATA_DIR, "seen.json")
LOG_FILE = os.path.join(DATA_DIR, "day_log.json")
ALERTED_FILE = os.path.join(DATA_DIR, "alerted.json")
BASELINE_FILE = os.path.join(DATA_DIR, "baseline.json")
RESULTS_FILE = os.path.join(DATA_DIR, "results_sent.json")
JOLPICA = "https://api.jolpi.ca/ergast/f1"

# Sırayla denenir; bir model 404/403 verirse (kapatılmış ya da erişim yok) sonrakine geçilir.
# GEMINI_MODEL tanımlıysa o en başa alınır.
MODELS = [m for m in [
    os.environ.get("GEMINI_MODEL"),
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-2.5-flash",
] if m]
_dead_models = set()
THRESHOLD = float(os.environ.get("ALERT_THRESHOLD", "8"))  # bu puan ve üstü anında bildirim
MAX_AGE_HOURS = int(os.environ.get("MAX_AGE_HOURS", "12"))  # bundan eski haberleri atla
MAX_ITEMS_PER_CAT = 40
BATCH = 20
MAX_ALERTS_PER_RUN = 5
DIGEST_TOP_N = 8
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "application/rss+xml, application/xml, text/xml, */*"}

CATEGORIES = {
    "araba": {
        "label": "Araba",
        "emoji": "🚗",
        "topic_env": "NTFY_CAR_TOPIC",
        "feeds": {
            "Motor1": "https://www.motor1.com/rss/articles/all/",
            "Autoblog": "https://www.autoblog.com/rss.xml",
            "Car and Driver": "https://www.caranddriver.com/rss/all.xml/",
            "The Drive": "https://www.thedrive.com/feed",
            "Autocar": "https://www.autocar.co.uk/rss",
            "Jalopnik": "https://jalopnik.com/rss",
        },
        "criteria": (
            "Yüksek (8-10): büyük markanın yeni model tanıtımı/dünya prömiyeri, performans ve spor "
            "araba haberleri, geri çağırma/güvenlik krizi, büyük sektör haberi (satın alma, fabrika, "
            "tarife, yasa), çığır açan teknoloji. "
            "Orta (4-7): test sürüşü, fiyat/donanım güncellemesi, elektrikli araç haberleri. "
            "Düşük (1-3): kampanya, ikinci el rehberi, liste yazıları, magazin."
        ),
    },
    "f1": {
        "label": "Formula 1",
        "emoji": "🏎️",
        "topic_env": "NTFY_F1_TOPIC",
        "feeds": {
            "Autosport": "https://www.autosport.com/rss/f1/news",
            "Motorsport.com": "https://www.motorsport.com/rss/f1/news/",
            "RaceFans": "https://www.racefans.net/feed/",
            "The Race": "https://www.the-race.com/category/formula-1/feed/",
            "Formula1.com": "https://www.formula1.com/content/fom-website/en/latest/all.xml",
        },
        "criteria": (
            "Yüksek (8-10): yarış/sıralama/sprint sonucu, şampiyonluk durumu, diskalifiye veya ceza, "
            "büyük kaza, pilot transferi ve sözleşme, takım patronu/ekip değişikliği, regülasyon kararı. "
            "Orta (4-7): yarış öncesi analiz, araç güncellemesi, röportaj. "
            "Düşük (1-3): spekülasyon, tarihî yazı, magazin, video/liste içerikleri."
        ),
    },
}


# ---------- yardımcılar ----------

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def clean(text, limit=400):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def short_bytes(text, limit=3900):
    return text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


# ---------- haber çekme ----------

def fetch_items(cfg, seen=None):
    """RSS kaynaklarından haberleri çeker. Daha önce işlenenleri (seen) en başta eler."""
    seen = seen or {}
    now = time.time()
    items, seen_ids = [], set()
    for source, url in cfg["feeds"].items():
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            if resp.status_code != 200:
                print(f"[uyarı] {source}: HTTP {resp.status_code} (site engellemiş ya da adres değişmiş olabilir)")
                continue
            feed = feedparser.parse(resp.content)
        except Exception as e:  # noqa: BLE001
            print(f"[uyarı] {source} okunamadı: {e}")
            continue
        if not feed.entries:
            print(f"[uyarı] {source}: haber bulunamadı (boş ya da bozuk yanıt)")
            continue
        for e in feed.entries:
            link = e.get("link")
            title = clean(e.get("title"), 200)
            if not link or not title:
                continue
            parsed = e.get("published_parsed") or e.get("updated_parsed")
            undated = not parsed  # bazı kaynaklar (örn. formula1.com) tarih vermiyor
            ts = calendar.timegm(parsed) if parsed else now
            if now - ts > MAX_AGE_HOURS * 3600:
                continue
            iid = hashlib.sha1(link.encode()).hexdigest()[:16]
            if iid in seen_ids or iid in seen:
                continue
            seen_ids.add(iid)
            items.append({
                "id": iid,
                "title": title,
                "desc": clean(e.get("summary") or e.get("description")),
                "link": link,
                "source": source,
                "ts": ts,
                "undated": undated,
            })
    items.sort(key=lambda x: x["ts"], reverse=True)
    return items[:MAX_ITEMS_PER_CAT]


# ---------- Gemini ----------

def gemini_json(prompt):
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }
    headers = {"x-goog-api-key": os.environ["GEMINI_API_KEY"]}
    last_err = "yanıt yok"
    for model in MODELS:
        if model in _dead_models:
            continue
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(3):
            r = requests.post(url, headers=headers, json=body, timeout=90)
            if r.status_code in (429, 500, 503):
                print(f"[bilgi] Gemini {model} {r.status_code}, bekleniyor...")
                last_err = f"{model}: HTTP {r.status_code}"
                time.sleep(15 * (attempt + 1))
                continue
            if r.status_code in (400, 403, 404):
                msg = r.text[:200].replace("\n", " ")
                print(f"[bilgi] {model} kullanılamıyor (HTTP {r.status_code}): {msg}")
                last_err = f"{model}: HTTP {r.status_code}"
                if r.status_code == 400 and "API key" in r.text:
                    raise RuntimeError("Gemini API anahtarı geçersiz (GEMINI_API_KEY secret'ını kontrol et)")
                _dead_models.add(model)
                break
            r.raise_for_status()
            try:
                text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
                data = json.loads(text)
            except (KeyError, IndexError, ValueError) as e:
                last_err = f"{model}: bozuk yanıt ({e})"
                print(f"[bilgi] {last_err}")
                break
            print(f"[bilgi] model: {model}")
            return data
    raise RuntimeError(f"hiçbir Gemini modeli yanıt vermedi ({last_err})")


def slugify(text):
    text = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return text[:60]


def score_items(cfg, items, known_keys=()):
    """Haberleri toplu olarak Gemini'ye gönderir. [(item, sonuç), ...] döner."""
    results = []
    known = list(known_keys)
    for start in range(0, len(items), BATCH):
        batch = items[start:start + BATCH]
        lines = "\n".join(f"[{i}] {it['title']} | {it['desc']}" for i, it in enumerate(batch))
        prompt = f"""Sen deneyimli bir {cfg['label']} haber editörüsün. Aşağıdaki haberleri değerlendir.

Her haber için şunları üret:
- "baslik": Türkçe, kısa ve net başlık
- "ozet": 1-2 cümlelik Türkçe özet (haberde olmayan bilgi ekleme)
- "puan": 1 ile 10 arasında önem puanı (ondalıklı olabilir)
- "grup": haberin konusunu anlatan kısa İngilizce anahtar (küçük harf, tire ile, en fazla 6 kelime, örn. "singapore-gp-pole-verstappen"). Farklı sitelerde yer alsa da AYNI olayı/konuyu anlatan haberlere BİREBİR AYNI anahtarı ver.
Daha önce kullanılmış anahtarlar: {', '.join(known[-60:]) if known else '(yok)'}
Bir haber bu listedeki bir olayla aynıysa o anahtarı aynen kullan, yeni bir olaysa yeni anahtar üret.

Puanlama kriterleri: {cfg['criteria']}
Puanlamada cimri ol: 8 ve üstü sadece gerçekten önemli haberlere verilsin, haberlerin çoğu 3-6 arasında olsun.

Haberler:
{lines}

Sadece şu biçimde bir JSON dizisi döndür, başka hiçbir şey yazma:
[{{"i": 0, "baslik": "...", "ozet": "...", "puan": 6.5, "grup": "..."}}]"""
        try:
            data = gemini_json(prompt)
        except Exception as e:  # noqa: BLE001
            print(f"[hata] {cfg['label']} toplu işlem başarısız: {e}")
            continue
        for r in data:
            try:
                item = batch[int(r["i"])]
                grup = slugify(str(r.get("grup", ""))) or item["id"]
                results.append((item, {
                    "baslik": str(r["baslik"]).strip(),
                    "ozet": str(r["ozet"]).strip(),
                    "puan": max(1.0, min(10.0, float(r["puan"]))),
                    "grup": grup,
                }))
                if grup not in known:
                    known.append(grup)
            except (KeyError, ValueError, IndexError, TypeError):
                continue
        time.sleep(4)  # ücretsiz katman hız sınırına takılmamak için
    return results


# ---------- ntfy ----------

def send_ntfy(topic, title, message, priority=3, click=None, tags=None):
    payload = {"topic": topic, "title": title, "message": short_bytes(message), "priority": priority}
    if click:
        payload["click"] = click
    if tags:
        payload["tags"] = tags
    r = requests.post(NTFY_SERVER, json=payload, timeout=30)
    r.raise_for_status()


# ---------- modlar ----------

def run_check():
    seen = load_json(SEEN_FILE, {})
    log = load_json(LOG_FILE, [])
    alerted = load_json(ALERTED_FILE, {})
    baseline = set(load_json(BASELINE_FILE, []))
    now = time.time()
    alerts = []

    for key, cfg in CATEGORIES.items():
        raw = fetch_items(cfg, seen)
        # Tarih vermeyen kaynakta (örn. formula1.com) ilk görüşte mevcut haberler eski olabilir:
        # onları işlemeden "görüldü" say, sadece bundan sonra gelenleri değerlendir.
        items = []
        for it in raw:
            if it["undated"] and it["source"] not in baseline:
                seen[it["id"]] = now
                continue
            items.append(it)
        baseline |= {it["source"] for it in raw if it["undated"]}
        print(f"[{key}] yeni haber: {len(items)}")
        if not items:
            continue
        known = sorted({e["grup"] for e in log
                        if e["cat"] == key and e.get("grup") and now - e["ts"] < 24 * 3600}
                       | {k.split("|", 1)[1] for k in alerted if k.startswith(key + "|")})
        for item, res in score_items(cfg, items, known):
            seen[item["id"]] = now
            entry = {
                "cat": key,
                "baslik": res["baslik"],
                "ozet": res["ozet"],
                "puan": res["puan"],
                "grup": res["grup"],
                "link": item["link"],
                "source": item["source"],
                "ts": now,
            }
            log.append(entry)
            if res["puan"] >= THRESHOLD:
                alerts.append(entry)

    # eskileri temizle
    seen = {k: v for k, v in seen.items() if now - v < 30 * 86400}
    log = [e for e in log if now - e["ts"] < 48 * 3600]
    save_json(SEEN_FILE, seen)
    save_json(LOG_FILE, log)
    save_json(BASELINE_FILE, sorted(baseline))

    # Aynı olayı anlatan haberlerden sadece en yüksek puanlıyı gönder;
    # son 48 saatte zaten bildirimi gönderilmiş konuları atla.
    best = {}
    for e in alerts:
        akey = f"{e['cat']}|{e['grup']}"
        if akey in alerted:
            print(f"[atlandı] zaten bildirildi: {e['grup']}")
            continue
        if akey not in best or e["puan"] > best[akey]["puan"]:
            best[akey] = e
    alerts = list(best.values())

    alerts.sort(key=lambda e: e["puan"], reverse=True)
    night = dt.datetime.now(TZ).hour < 8  # gece sessiz bildirim
    for e in alerts[:MAX_ALERTS_PER_RUN]:
        cfg = CATEGORIES[e["cat"]]
        topic = os.environ.get(cfg["topic_env"])
        if not topic:
            print(f"[uyarı] {cfg['topic_env']} tanımlı değil, bildirim atlandı")
            continue
        priority = 2 if night else (4 if e["puan"] >= 9 else 3)
        message = f"{e['ozet']}\n\n⭐ Önem: {e['puan']:.1f}/10\n📰 {e['source']}"
        try:
            send_ntfy(topic, f"{cfg['emoji']} {e['baslik']}", message,
                      priority=priority, click=e["link"], tags=["rotating_light"] if e["puan"] >= 9 else None)
            alerted[f"{e['cat']}|{e['grup']}"] = now
            print(f"[bildirim] {e['baslik']} ({e['puan']:.1f})")
        except Exception as ex:  # noqa: BLE001
            print(f"[hata] bildirim gönderilemedi: {ex}")
    alerted = {k: v for k, v in alerted.items() if now - v < 48 * 3600}
    save_json(ALERTED_FILE, alerted)

    try:
        run_results()
    except Exception as ex:  # noqa: BLE001
        print(f"[hata] F1 sonuç kartı: {ex}")


# ---------- F1 sonuç kartı (Jolpica / Ergast verisi) ----------

def _jolpica(path):
    r = requests.get(f"{JOLPICA}/{path}", headers=HEADERS, timeout=30)
    if r.status_code != 200:
        print(f"[uyarı] Jolpica {path}: HTTP {r.status_code}")
        return None
    races = r.json().get("MRData", {}).get("RaceTable", {}).get("Races", [])
    return races[0] if races else None


def _best_q(res):
    return res.get("Q3") or res.get("Q2") or res.get("Q1") or "-"


def format_results(kind, race):
    rows = race.get("QualifyingResults" if kind == "qualifying" else
                    "SprintResults" if kind == "sprint" else "Results", [])
    lines = []
    for r in rows:
        d, c = r["Driver"], r["Constructor"]["name"]
        name = d.get("familyName", "?")
        pos = r.get("positionText") or r.get("position", "?")
        if kind == "qualifying":
            lines.append(f"{r['position']}. {name} ({c}) {_best_q(r)}")
        else:
            gap = (r.get("Time") or {}).get("time") or r.get("status", "")
            grid = r.get("grid")
            start = f" · P{grid}'den" if grid and grid != "0" else (" · pit çıkışı" if grid == "0" else "")
            lines.append(f"{pos}. {name} ({c}) {gap}{start}")
    return lines


def run_results():
    """Sıralama / sprint / yarış sonuçları yayınlandıysa bir kez bildirim gönderir."""
    topic = os.environ.get(CATEGORIES["f1"]["topic_env"])
    if not topic:
        return
    sent = load_json(RESULTS_FILE, {})
    today = dt.datetime.now(TZ).date()
    titles = {"qualifying": "Sıralama Turları", "sprint": "Sprint Sonucu", "results": "Yarış Sonucu"}
    for kind in ("qualifying", "sprint", "results"):
        race = _jolpica(f"current/last/{kind}.json")
        if not race:
            continue
        try:
            race_date = dt.date.fromisoformat(race["date"])
        except (KeyError, ValueError):
            continue
        if not (today - dt.timedelta(days=3) <= race_date <= today + dt.timedelta(days=2)):
            continue  # eski hafta sonu, ilk kurulumda eski sonuçları gönderme
        skey = f"{race['season']}-{race['round']}-{kind}"
        if skey in sent:
            continue
        lines = format_results(kind, race)
        if not lines:
            continue
        if kind == "qualifying":
            lines.append("")
            lines.append("ℹ️ Ceza alan pilotlar start sırasında geriye düşebilir; "
                         "kesin grid yarış sonrası tabloda görünür.")
        title = f"🏁 {race['raceName']} - {titles[kind]}"
        send_ntfy(topic, title, "\n".join(lines), priority=4, click=race.get("url"))
        sent[skey] = time.time()
        print(f"[sonuç] {title}")
    # çok eski kayıtları temizle
    sent = {k: v for k, v in sent.items() if time.time() - v < 30 * 86400}
    save_json(RESULTS_FILE, sent)


def run_digest():
    log = load_json(LOG_FILE, [])
    now = time.time()
    today = dt.datetime.now(TZ).strftime("%d.%m.%Y")
    for key, cfg in CATEGORIES.items():
        topic = os.environ.get(cfg["topic_env"])
        if not topic:
            print(f"[uyarı] {cfg['topic_env']} tanımlı değil, özet atlandı")
            continue
        entries = [e for e in log if e["cat"] == key and now - e["ts"] < 24 * 3600]
        entries.sort(key=lambda e: e["puan"], reverse=True)
        uniq = {}
        for e in entries:  # aynı konudan sadece en yüksek puanlı haber
            uniq.setdefault(e.get("grup") or e["link"], e)
        entries = list(uniq.values())
        important = sum(1 for e in entries if e["puan"] >= THRESHOLD)
        if entries:
            lines = [f"📅 {today} Tarihli Günlük Özet", ""]
            for n, e in enumerate(entries[:DIGEST_TOP_N], 1):
                lines.append(f"{n}. {e['baslik']} ⭐{e['puan']:.1f}")
                lines.append(f"   {e['ozet']}")
                lines.append(f"   {e['link']}")
                lines.append("")
            lines += ["📊 İstatistikler:",
                      f"- Toplam haber: {len(entries)}",
                      f"- Önemli haber: {important}"]
            body = "\n".join(lines)
        else:
            body = f"📅 {today}\nSon 24 saatte yeni haber bulunamadı."
        try:
            send_ntfy(topic, f"{cfg['emoji']} {cfg['label']} Haberleri - 24 Saatlik Özet", body, priority=3)
            print(f"[özet] {key} gönderildi")
        except Exception as ex:  # noqa: BLE001
            print(f"[hata] özet gönderilemedi: {ex}")


DIGEST_STATE_FILE = os.path.join(DATA_DIR, "digest_state.json")
DIGEST_AT = (21, 5)  # her gün bu saatten sonraki ilk çalışmada günlük özet gider


def maybe_digest():
    """GitHub zamanlayıcısı bir çalışmayı atlasa da özet o gün mutlaka bir kez gönderilir."""
    now = dt.datetime.now(TZ)
    if (now.hour, now.minute) < DIGEST_AT:
        return
    today = now.strftime("%Y-%m-%d")
    if load_json(DIGEST_STATE_FILE, {}).get("last") == today:
        return
    run_digest()
    save_json(DIGEST_STATE_FILE, {"last": today})


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    if mode == "digest":
        run_digest()  # elle çalıştırma: günlük otomatik özeti etkilemez
    else:
        run_check()
        maybe_digest()
