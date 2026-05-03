import os
import json
import time
import hashlib
from datetime import datetime, timezone
from dateutil import tz
import feedparser
import requests

BRT = tz.gettz("America/Sao_Paulo")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
IS_DIGEST_RUN = os.environ.get("IS_DIGEST_RUN", "").lower() == "true"

STATE_PATH = "state.json"
SOURCES_PATH = "sources.json"

LAUNCH_KEYWORDS = [
    "launch", "liftoff", "scrub", "hold", "countdown", "static fire",
    "rocket", "falcon", "starship", "new glenn", "electron", "neutron",
    "ariane", "vega", "atlas", "vulcan", "sls", "soyuz", "long march",
    "launches", "launched", "launch attempt", "t-0", "webcast",
    "lançamento", "decolagem", "adiado", "adiamento", "contagem regressiva"
]

ANOMALY_KEYWORDS = [
    "anomaly", "failure", "explosion", "abort", "incident", "mishap",
    "loss of signal", "engine out", "off-nominal", "issue", "leak",
    "emergency", "pad abort", "in-flight abort",
    "anomalia", "falha", "explosão", "aborto", "incidente", "problema",
    "vazamento", "emergência"
]

ASTRO_KEYWORDS = [
    "jwst", "webb", "hubble", "chandra", "x-ray", "exoplanet", "supernova",
    "black hole", "gravitational waves", "ligo", "virgo", "kilonova",
    "astronomy", "astrophysics", "cosmology", "galaxy", "nebula",
    "dark matter", "dark energy", "quasar", "pulsar", "magnetar",
    "astronomia", "astrofísica", "cosmologia", "galáxia", "buraco negro",
    "matéria escura", "energia escura"
]

BREAKING_KEYWORDS = ["breaking", "urgent", "update", "live", "now", "just in", "confirmed", "official"]

def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

def norm(text):
    return (text or "").strip().lower()

def fingerprint(entry):
    base = (entry.get("link") or "") + "||" + (entry.get("title") or "")
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:24]

def score_entry(title, summary, source_weight):
    blob = norm(title) + " " + norm(summary)

    is_launch = any(k in blob for k in LAUNCH_KEYWORDS)
    is_anomaly = any(k in blob for k in ANOMALY_KEYWORDS)
    is_astro = any(k in blob for k in ASTRO_KEYWORDS)
    is_breaking = any(k in blob for k in BREAKING_KEYWORDS)

    score = 0.0
    if is_launch: score += 4.0
    if is_anomaly: score += 6.0
    if is_astro: score += 2.0
    if is_breaking: score += 1.5

    intensifiers = {
        "scrub": 3.0, "scrubbed": 3.0, "delayed": 2.0, "postponed": 2.0,
        "explosion": 5.0, "abort": 3.0, "failure": 4.0,
        "adiado": 2.5, "explosão": 5.0, "falha": 4.0,
        "discovery": 2.0, "descoberta": 2.0
    }
    for k, w in intensifiers.items():
        if k in blob:
            score += w

    score *= float(source_weight)
    return score, is_launch, is_anomaly

def escape_html(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def telegram_send(text, silent=False):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "disable_web_page_preview": False,
        "parse_mode": "HTML",
        "disable_notification": silent
    }
    r = requests.post(url, data=payload, timeout=20)
    r.raise_for_status()

def now_brt():
    return datetime.now(timezone.utc).astimezone(BRT)

def entry_published_dt(entry):
    """
    Tenta obter datetime com timezone a partir do RSS/Atom.
    Retorna datetime em UTC (tz-aware) ou None se não der.
    """
    st = entry.get("published_parsed") or entry.get("updated_parsed")
    if not st:
        return None
    # struct_time -> timestamp -> datetime UTC
    return datetime.fromtimestamp(time.mktime(st), tz=timezone.utc)

def is_today_brt(dt_utc):
    """
    dt_utc: datetime tz-aware em UTC
    """
    if dt_utc is None:
        return False
    dt_brt = dt_utc.astimezone(BRT)
    today = now_brt().date()
    return dt_brt.date() == today


def main():
    state = load_json(STATE_PATH, {"seen": {}, "digest_queue": []})
    sources = load_json(SOURCES_PATH, {"feeds": []})["feeds"]

    new_items = []
    for src in sources:
        feed = feedparser.parse(src["url"])
        for e in feed.entries[:20]:
            fp = fingerprint(e)
            if fp in state["seen"]:
                continue

            title = (e.get("title") or "").strip()
            summary = e.get("summary") or e.get("description") or ""
            link = (e.get("link") or "").strip()
            if not title or not link:
                continue

            score, is_launch, is_anomaly = score_entry(title, summary, src.get("weight", 0.7))

            state["seen"][fp] = {"ts": int(time.time()), "source": src["name"], "title": title, "link": link, "score": score}

            new_items.append({
                "title": title, "link": link, "source": src["name"], "score": score,
                "is_launch": is_launch, "is_anomaly": is_anomaly
            })

    new_items.sort(key=lambda x: x["score"], reverse=True)

    # Alertas imediatos (somente no run de 10 min)
    if not IS_DIGEST_RUN:
        immediate = [x for x in new_items if (x["is_launch"] or x["is_anomaly"]) and x["score"] >= 5.5]
        for x in immediate[:8]:
            if x["is_launch"] and x["is_anomaly"]:
                cat = "🚨 ALERTA CRÍTICO"
            elif x["is_anomaly"]:
                cat = "⚠️ ANOMALIA"
            else:
                cat = "🚀 LANÇAMENTO"

            msg = (
                f"{cat} <b>{escape_html(x['title'])}</b>\n\n"
                f"📡 <i>{escape_html(x['source'])}</i> | Score: {x['score']:.1f}\n"
                f"🔗 {escape_html(x['link'])}"
            )
            telegram_send(msg)

    # Fila do digest
    for x in new_items:
        if x["score"] >= 2.0:
            state["digest_queue"].append({**x, "ts": int(time.time())})

    # Digest (somente nos runs 08:00/20:00 BRT)
    if IS_DIGEST_RUN:
        cutoff = int(time.time()) - 60 * 60 * 16
        recent = [x for x in state["digest_queue"] if x["ts"] >= cutoff]
        recent.sort(key=lambda x: x["score"], reverse=True)
        top = recent[:12]

        dt = now_brt()
        period = "🌅 MANHÃ" if dt.hour < 15 else "🌙 NOITE"

        if top:
            lines = [
                f"🛰️ <b>Digest Astronomia & Espaço</b> - {period}",
                f"📅 {dt.strftime('%d/%m/%Y às %H:%M')} (BRT)\n"
            ]
            for i, x in enumerate(top, 1):
                lines.append(
                    f"{i}) <b>{escape_html(x['title'])}</b>\n"
                    f"📡 <i>{escape_html(x['source'])}</i> | {x['score']:.1f}\n"
                    f"🔗 {escape_html(x['link'])}\n"
                )
            telegram_send("\n".join(lines), silent=True)

        state["digest_queue"] = [x for x in state["digest_queue"] if x["ts"] >= cutoff]

    # Enxuga histórico
    if len(state["seen"]) > 8000:
        items = list(state["seen"].items())
        items.sort(key=lambda kv: kv[1]["ts"], reverse=True)
        state["seen"] = dict(items[:6000])

    save_json(STATE_PATH, state)
    print(f"Done. New items: {len(new_items)} | Digest run: {IS_DIGEST_RUN}")

if __name__ == "__main__":
    main()
