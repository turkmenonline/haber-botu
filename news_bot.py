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

MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
THRESHOLD = float(os.environ.get("ALERT_THRESHOLD", "8"))  # bu puan ve üstü anında bildirim
MAX_AGE_HOURS = int(os.environ.get("MAX_AGE_HOURS", "12"))  # bundan eski haberleri atla
MAX_ITEMS_PER_CAT = 40
BATCH = 20
MAX_ALERTS_PER_RUN = 5
DIGEST_TOP_N = 8
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
UA = "Mozilla/5.0 (compatible; HaberBotu/1.0)"

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

def fetch_items(cfg):
    now = time.time()
    items, seen_ids = [], set()
    for source, url in cfg["feeds"].items():
        try:
            feed = feedparser.parse(url, agent=UA)
        except Exception as e:  # noqa: BLE001
            print(f"[uyarı] {source} okunamadı: {e}")
            continue
        if not feed.entries:
            print(f"[uyarı] {source}: haber bulunamadı (adres değişmiş olabilir)")
            continue
        for e in feed.entries:
            link = e.get("link")
            title = clean(e.get("title"), 200)
            if not link or not title:
                continue
            parsed = e.get("published_parsed") or e.get("updated_parsed")
            ts = calendar.timegm(parsed) if parsed else now
            if now - ts > MAX_AGE_HOURS * 3600:
                continue
            iid = hashlib.sha1(link.encode()).hexdigest()[:16]
            if iid in seen_ids:
                continue
            seen_ids.add(iid)
            items.append({
                "id": iid,
                "title": title,
                "desc": clean(e.get("summary") or e.get("description")),
                "link": link,
                "source": source,
                "ts": ts,
            })
    items.sort(key=lambda x: x["ts"], reverse=True)
    return items[:MAX_ITEMS_PER_CAT]


# ---------- Gemini ----------

def gemini_json(prompt):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }
    headers = {"x-goog-api-key": os.environ["GEMINI_API_KEY"]}
    for attempt in range(4):
        r = requests.post(url, headers=headers, json=body, timeout=90)
        if r.status_code in (429, 500, 503):
            print(f"[bilgi] Gemini {r.status_code}, bekleniyor...")
            time.sleep(15 * (attempt + 1))
            continue
        r.raise_for_status()
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
        return json.loads(text)
    raise RuntimeError("Gemini yanıt vermedi")


def score_items(cfg, items):
    """Haberleri toplu olarak Gemini'ye gönderir. [(item, sonuç), ...] döner."""
    results = []
    for start in range(0, len(items), BATCH):
        batch = items[start:start + BATCH]
        lines = "\n".join(f"[{i}] {it['title']} | {it['desc']}" for i, it in enumerate(batch))
        prompt = f"""Sen deneyimli bir {cfg['label']} haber editörüsün. Aşağıdaki haberleri değerlendir.

Her haber için şunları üret:
- "baslik": Türkçe, kısa ve net başlık
- "ozet": 1-2 cümlelik Türkçe özet (haberde olmayan bilgi ekleme)
- "puan": 1 ile 10 arasında önem puanı (ondalıklı olabilir)

Puanlama kriterleri: {cfg['criteria']}
Puanlamada cimri ol: 8 ve üstü sadece gerçekten önemli haberlere verilsin, haberlerin çoğu 3-6 arasında olsun.

Haberler:
{lines}

Sadece şu biçimde bir JSON dizisi döndür, başka hiçbir şey yazma:
[{{"i": 0, "baslik": "...", "ozet": "...", "puan": 6.5}}]"""
        try:
            data = gemini_json(prompt)
        except Exception as e:  # noqa: BLE001
            print(f"[hata] {cfg['label']} toplu işlem başarısız: {e}")
            continue
        for r in data:
            try:
                item = batch[int(r["i"])]
                results.append((item, {
                    "baslik": str(r["baslik"]).strip(),
                    "ozet": str(r["ozet"]).strip(),
                    "puan": max(1.0, min(10.0, float(r["puan"]))),
                }))
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
    now = time.time()
    alerts = []

    for key, cfg in CATEGORIES.items():
        items = [it for it in fetch_items(cfg) if it["id"] not in seen]
        print(f"[{key}] yeni haber: {len(items)}")
        if not items:
            continue
        for item, res in score_items(cfg, items):
            seen[item["id"]] = now
            entry = {
                "cat": key,
                "baslik": res["baslik"],
                "ozet": res["ozet"],
                "puan": res["puan"],
                "link": item["link"],
                "source": item["source"],
                "ts": now,
            }
            log.append(entry)
            if res["puan"] >= THRESHOLD:
                alerts.append(entry)

    # eskileri temizle
    seen = {k: v for k, v in seen.items() if now - v < 7 * 86400}
    log = [e for e in log if now - e["ts"] < 48 * 3600]
    save_json(SEEN_FILE, seen)
    save_json(LOG_FILE, log)

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
            print(f"[bildirim] {e['baslik']} ({e['puan']:.1f})")
        except Exception as ex:  # noqa: BLE001
            print(f"[hata] bildirim gönderilemedi: {ex}")


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


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    if mode == "digest":
        run_digest()
    else:
        run_check()
