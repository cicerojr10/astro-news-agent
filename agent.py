import os
import json
import time
import hashlib
from datetime import datetime, timezone
from dateutil import tz
import feedparser
import requests

BRT = tz.gettz("America/Sao_Paulo")

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

BREAKING_KEYWORDS = [
    "breaking", "urgent", "update", "live", "now", "just in", "confirmed", "official"
]


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
    if is_launch:
        score += 4.0
    if is_anomaly:
        score += 6.0
    if is_astro:
        score += 2.0
    if is_breaking:
        score += 1.5

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


def now_brt():
    return datetime.now(timezone.utc).astimezone(BRT)


def entry_published_dt_utc(entry):
    """
    Retorna datetime tz-aware em UTC baseado em published/updated do feed,
    ou None se o feed não fornecer.
    """
    st = entry.get("published_parsed") or entry.get("updated_parsed")
    if not st:
        return None
    return datetime.fromtimestamp(time.mktime(st), tz=timezone.utc)


def is_today_brt(dt_utc):
    if dt_utc is None:
        return False
    return dt_utc.astimezone(BRT).date() == now_brt().date()


def is_digest_time_window_brt():
    """
    Considera digest se estiver perto do horário agendado (tolerância).
    Isso evita depender do "qual cron disparou" no GitHub.
    """
    dt = now_brt()
    # janela de 20 min para cobrir atrasos do scheduler
    if dt.hour == 8 and 0 <= dt.minute <= 20:
        return True
    if dt.hour == 20 and 0 <= dt.minute <= 20:
        return True
    return False


def telegram_send(text, silent=False):
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": False,
        "parse_mode": "HTML",
        "disable_notification": silent
    }
    r = requests.post(url, data=payload, timeout=20)
    r.raise_for_status()


def main():
    state = load_json(STATE_PATH, {"seen": {}, "digest_queue": []})
    sources = load_json(SOURCES_PATH, {"feeds": []}).get("feeds", [])

    is_digest_run = is_digest_time_window_brt()
    today = now_brt().date()

    new_items = []

    for src in sources:
        url = src["url"]
        name = src.get("name", url)
        weight = src.get("weight", 0.7)

        feed = feedparser.parse(url)

        # se feed estiver com erro, não derruba o job inteiro
        if getattr(feed, "bozo", False):
            # bozo_exception pode existir; só loga
            print(f"[WARN] Feed bozo: {name} | {url}")
            continue

        for e in feed.entries[:50]:
            pub_dt_utc = entry_published_dt_utc(e)

            # Regra: só notícia de HOJE (BRT)
            if not is_today_brt(pub_dt_utc):
                continue

            fp = fingerprint(e)
            if fp in state["seen"]:
                continue

            title = (e.get("title") or "").strip()
            summary = e.get("summary") or e.get("description") or ""
            link = (e.get("link") or "").strip()
            if not title or not link:
                continue

            score, is_launch, is_anomaly = score_entry(title, summary, weight)

            state["seen"][fp] = {
                "ts": int(time.time()),  # hora que vimos
                "source": name,
                "title": title,
                "link": link,
                "score": score,
                "published_utc": pub_dt_utc.isoformat() if pub_dt_utc else None
            }

            item = {
                "title": title,
                "link": link,
                "source": name,
                "score": score,
                "is_launch": is_launch,
                "is_anomaly": is_anomaly,
                "published_utc": pub_dt_utc.isoformat() if pub_dt_utc else None,
                "day_brt": str(today)
            }
            new_items.append(item)

    new_items.sort(key=lambda x: x["score"], reverse=True)

    # Alertas imediatos (runs de 10 min, não digest)
    if not is_digest_run:
        immediate = [
            x for x in new_items
            if (x["is_launch"] or x["is_anomaly"]) and x["score"] >= 5.5
        ]
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

    # Enfileira para digest (somente itens de hoje já passaram pelo filtro)
    for x in new_items:
        if x["score"] >= 2.0:
            state["digest_queue"].append({**x, "queued_ts": int(time.time())})

    # Digest (08:00/20:00 BRT)
    if is_digest_run:
        dt = now_brt()
        period = "🌅 MANHÃ" if dt.hour < 15 else "🌙 NOITE"

        queue = list(state["digest_queue"])
        queue.sort(key=lambda x: x["score"], reverse=True)
        top = queue[:12]

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
        else:
            # Opcional: mandar “sem notícias hoje”
            # telegram_send(
            #     f"🛰️ <b>Digest Astronomia & Espaço</b>\n📅 {dt.strftime('%d/%m/%Y')} (BRT)\n\nSem notícias novas de hoje nas fontes monitoradas.",
            #     silent=True
            # )
            pass

        # limpa a fila após digest para não repetir
        state["digest_queue"] = []

    # Enxuga histórico
    if len(state["seen"]) > 8000:
        items = list(state["seen"].items())
        items.sort(key=lambda kv: kv[1]["ts"], reverse=True)
        state["seen"] = dict(items[:6000])

    save_json(STATE_PATH, state)

    print(
        f"Done. Today(BRT): {today} | Digest run: {is_digest_run} | "
        f"New(today) items: {len(new_items)} | Queue size: {len(state['digest_queue'])}"
    )


if __name__ == "__main__":
    main()
