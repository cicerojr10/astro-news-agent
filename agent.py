import os
import json
import time
import re
import hashlib
from datetime import datetime, timezone
from dateutil import tz
import feedparser
import requests
from bs4 import BeautifulSoup

BRT = tz.gettz("America/Sao_Paulo")

DEBUG = os.environ.get("DEBUG", "").lower() == "true"
DEBUG_TELEGRAM = os.environ.get("DEBUG_TELEGRAM", "").lower() == "true"

STATE_PATH = "state.json"
SOURCES_PATH = "sources.json"

# ====== FOCO: LANÇAMENTOS / EMPRESAS / OPERAÇÕES ======

LAUNCH_COMPANY_KEYWORDS = [
    # launch / ops
    "launch", "launched", "liftoff", "scrub", "scrubbed", "hold", "countdown",
    "static fire", "wet dress", "rollout", "rollback", "webcast", "live coverage",
    "launch attempt", "t-0", "mission", "rideshare",
    # vehicles / programs
    "falcon 9", "falcon heavy", "starship", "super heavy",
    "new glenn", "vulcan", "atlas v", "ariane 6", "vega", "electron", "neutron",
    "soyuz", "long march", "angara", "h3", "gslv", "pslv",
    # companies / orgs
    "spacex", "blue origin", "ula", "arianespace", "esa", "nasa",
    "roscosmos", "isro", "cnsa", "jaxa", "rocket lab",
    "amazon", "project kuiper", "kuiper", "oneweb", "viasat", "starlink",
    # payload/segment
    "satellite", "payload", "to orbit", "orbit", "leo", "geo", "ssso", "sso",
    # PT-BR
    "lançamento", "decolagem", "adiado", "adiamento", "contagem regressiva",
    "foguete", "órbita", "satélite", "missão"
]

ANOMALY_KEYWORDS = [
    "anomaly", "failure", "explosion", "abort", "incident", "mishap",
    "loss of signal", "engine out", "off-nominal", "issue", "leak",
    "emergency", "in-flight abort", "pad abort", "wrong orbit",
    "anomalia", "falha", "explosão", "aborto", "incidente", "problema",
    "vazamento", "emergência", "órbita errada"
]

# Bloqueia "lixo" típico do Space.com e afins
EXCLUDE_KEYWORDS = [
    "lego", "star wars", "best", "ranked", "podcast", "review",
    "photo of the day", "space photo of the day", "images", "wallpaper",
    "movie", "tv", "show", "episode", "entertainment", "sci-fi",
    "astrophotography", "skywatching", "meteor", "eclipse", "moon",
    "stargazing", "what's up", "whats up"
]

MIN_SCORE_SEND = 3.0
MIN_SCORE_DIGEST = 3.0
MIN_SCORE_ALERT = 5.5
MAX_UPDATES_PER_RUN = 3
MAX_ALERTS_PER_RUN = 8

HTTP_TIMEOUT = 20
UA = "Mozilla/5.0 (AstroNewsAgent/1.0; +https://github.com/)"

# ====== Utils ======

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

def escape_html(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def now_brt():
    return datetime.now(timezone.utc).astimezone(BRT)

def fingerprint(entry):
    base = (entry.get("link") or "") + "||" + (entry.get("title") or "")
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:24]

def entry_published_dt_utc(entry):
    st = entry.get("published_parsed") or entry.get("updated_parsed")
    if not st:
        return None
    return datetime.fromtimestamp(time.mktime(st), tz=timezone.utc)

def is_today_brt(dt_utc):
    if dt_utc is None:
        return False
    return dt_utc.astimezone(BRT).date() == now_brt().date()

def score_entry(title, summary, source_weight):
    blob = norm(title) + " " + norm(summary)

    has_focus = any(k in blob for k in LAUNCH_COMPANY_KEYWORDS)
    has_anomaly = any(k in blob for k in ANOMALY_KEYWORDS)
    has_exclude = any(k in blob for k in EXCLUDE_KEYWORDS)

    # base
    score = 0.0
    if has_focus:
        score += 4.0
    if has_anomaly:
        score += 6.0
    if has_exclude:
        score -= 6.0

    intensifiers = {
        "scrub": 3.0, "scrubbed": 3.0, "delayed": 2.0, "postponed": 2.0,
        "explosion": 6.0, "abort": 3.0, "failure": 5.0, "wrong orbit": 4.0,
        "adiado": 2.5, "explosão": 6.0, "falha": 5.0, "órbita errada": 4.0,
        "launch preview": 1.5, "live coverage": 1.2
    }
    for k, w in intensifiers.items():
        if k in blob:
            score += w

    score *= float(source_weight)
    return score, has_focus, has_anomaly, has_exclude

def relevant_item(title, summary):
    blob = norm(title) + " " + norm(summary)
    if any(k in blob for k in EXCLUDE_KEYWORDS):
        return False
    if any(k in blob for k in LAUNCH_COMPANY_KEYWORDS) or any(k in blob for k in ANOMALY_KEYWORDS):
        return True
    return False

# ====== HTML fetch + published time extraction ======

ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")

def parse_datetime_loose(s):
    if not s:
        return None
    s = s.strip()
    s2 = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass
    m = ISO_DATE_RE.search(s)
    if m:
        try:
            dt = datetime.fromisoformat(m.group(0) + "+00:00")
            return dt.astimezone(timezone.utc)
        except Exception:
            return None
    return None

def fetch_html(url):
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT, headers={"User-Agent": UA})
        r.raise_for_status()
        return r.text
    except Exception as e:
        if DEBUG:
            print(f"[DEBUG] fetch_html failed: {url} | {e}")
        return None

def extract_published_utc_from_html(html):
    if not html:
        return None

    soup = BeautifulSoup(html, "html.parser")

    # 1) OpenGraph
    for prop in ["article:published_time", "article:modified_time"]:
        tag = soup.find("meta", attrs={"property": prop})
        if tag and tag.get("content"):
            dt = parse_datetime_loose(tag["content"])
            if dt:
                return dt

    # 2) Meta name
    for name in ["pubdate", "publishdate", "date", "datePublished", "DC.date.issued"]:
        tag = soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            dt = parse_datetime_loose(tag["content"])
            if dt:
                return dt

    # 3) time datetime
    t = soup.find("time")
    if t and t.get("datetime"):
        dt = parse_datetime_loose(t["datetime"])
        if dt:
            return dt

    # 4) JSON-LD
    scripts = soup.find_all("script", attrs={"type": "application/ld+json"})
    for sc in scripts[:6]:
        try:
            data = json.loads(sc.get_text(strip=True) or "{}")
        except Exception:
            continue

        candidates = []
        if isinstance(data, list):
            candidates = data
        elif isinstance(data, dict):
            candidates = [data]

        for obj in candidates:
            if not isinstance(obj, dict):
                continue
            for key in ["datePublished", "dateModified"]:
                if key in obj:
                    dt = parse_datetime_loose(str(obj.get(key)))
                    if dt:
                        return dt

    return None

def extract_text_for_summary(html, max_chars=1800):
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    node = soup.find("article") or soup.body
    if not node:
        return ""

    text = node.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]

# ====== "Resumo PT-BR" (heurístico) ======

def ptbr_bullet_summary(title, text):
    if not text:
        return "Resumo: link com pouco texto extraível; confira a matéria para detalhes."

    parts = re.split(r"(?<=[\.\!\?])\s+", text)
    parts = [p.strip() for p in parts if len(p.strip()) >= 60]

    picks = parts[:2] if parts else [text[:220]]

    bullets = []
    for p in picks:
        bullets.append(p)

    return "Resumo (PT-BR): " + " ".join(bullets)

# ====== Telegram ======

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
    r = requests.post(url, data=payload, timeout=HTTP_TIMEOUT)
    r.raise_for_status()

# ====== Digest scheduling robusto ======

def should_send_digest(state, slot):
    today = str(now_brt().date())
    sent = state.get("digests_sent", {})
    return sent.get(today, {}).get(slot) != True

def mark_digest_sent(state, slot):
    today = str(now_brt().date())
    state.setdefault("digests_sent", {})
    state["digests_sent"].setdefault(today, {})
    state["digests_sent"][today][slot] = True

def in_digest_window():
    dt = now_brt()
    if 8 <= dt.hour < 12:
        return "morning"
    if 20 <= dt.hour < 23:
        return "night"
    return None

# ====== Main ======

def main():
    state = load_json(STATE_PATH, {"seen": {}, "digest_queue": [], "sent_updates": {}, "digests_sent": {}})
    sources = load_json(SOURCES_PATH, {"feeds": []}).get("feeds", [])

    today_brt = str(now_brt().date())

    stats = {
        "total_entries": 0,
        "skip_irrelevant": 0,
        "skip_no_title_link": 0,
        "skip_seen": 0,
        "skip_no_date": 0,
        "skip_not_today": 0,
        "skip_excluded": 0,
        "skip_not_focus": 0,
        "kept": 0,
        "alerts": 0,
        "updates_sent": 0
    }

    new_items = []

    for src in sources:
        url = src["url"]
        name = src.get("name", url)
        weight = src.get("weight", 0.7)

        feed = feedparser.parse(url)
        if getattr(feed, "bozo", False):
            print(f"[WARN] Feed bozo: {name} | {url}")
            continue

        for e in feed.entries[:60]:
            stats["total_entries"] += 1

            title = (e.get("title") or "").strip()
            summary = e.get("summary") or e.get("description") or ""
            link = (e.get("link") or "").strip()
            if not title or not link:
                stats["skip_no_title_link"] += 1
                continue

            if not relevant_item(title, summary):
                stats["skip_irrelevant"] += 1
                if DEBUG:
                    print(f"[DEBUG] skip_irrelevant: {name} | {title}")
                continue

            fp = fingerprint(e)
            if fp in state["seen"]:
                stats["skip_seen"] += 1
                continue

            pub_dt_utc = entry_published_dt_utc(e)

            html = None
            if pub_dt_utc is None:
                html = fetch_html(link)
                pub_dt_utc = extract_published_utc_from_html(html)

            if pub_dt_utc is None:
                stats["skip_no_date"] += 1
                state["seen"][fp] = {
                    "ts": int(time.time()),
                    "source": name,
                    "title": title,
                    "link": link,
                    "score": 0.0,
                    "published_utc": None,
                    "note": "no_published_date"
                }
                if DEBUG:
                    print(f"[DEBUG] skip_no_date: {name} | {title} | {link}")
                continue

            if not is_today_brt(pub_dt_utc):
                stats["skip_not_today"] += 1
                state["seen"][fp] = {
                    "ts": int(time.time()),
                    "source": name,
                    "title": title,
                    "link": link,
                    "score": 0.0,
                    "published_utc": pub_dt_utc.isoformat(),
                    "note": "not_today"
                }
                if DEBUG:
                    print(f"[DEBUG] skip_not_today: {name} | {title} | published: {pub_dt_utc}")
                continue

            score, has_focus, has_anomaly, has_exclude = score_entry(title, summary, weight)

            if has_exclude:
                stats["skip_excluded"] += 1
                state["seen"][fp] = {
                    "ts": int(time.time()),
                    "source": name,
                    "title": title,
                    "link": link,
                    "score": score,
                    "published_utc": pub_dt_utc.isoformat(),
                    "note": "excluded"
                }
                if DEBUG:
                    print(f"[DEBUG] skip_excluded: {name} | {title}")
                continue

            if not has_focus and not has_anomaly:
                stats["skip_not_focus"] += 1
                state["seen"][fp] = {
                    "ts": int(time.time()),
                    "source": name,
                    "title": title,
                    "link": link,
                    "score": score,
                    "published_utc": pub_dt_utc.isoformat(),
                    "note": "not_focus"
                }
                if DEBUG:
                    print(f"[DEBUG] skip_not_focus: {name} | {title}")
                continue

            if html is None:
                html = fetch_html(link)
            text = extract_text_for_summary(html) if html else ""
            resumo_pt = ptbr_bullet_summary(title, text)

            state["seen"][fp] = {
                "ts": int(time.time()),
                "source": name,
                "title": title,
                "link": link,
                "score": score,
                "published_utc": pub_dt_utc.isoformat()
            }

            item = {
                "fp": fp,
                "title": title,
                "link": link,
                "source": name,
                "score": score,
                "is_anomaly": has_anomaly,
                "published_utc": pub_dt_utc.isoformat(),
                "day_brt": today_brt,
                "resumo_pt": resumo_pt
            }
            new_items.append(item)
            stats["kept"] += 1

            if DEBUG:
                print(f"[DEBUG] kept: {name} | {title} | score: {score:.1f}")

    # ordena por relevância
    new_items.sort(key=lambda x: x["score"], reverse=True)

    # ===== Alertas imediatos =====
    alerts = [x for x in new_items if x["is_anomaly"] and x["score"] >= MIN_SCORE_ALERT]
    for x in alerts[:MAX_ALERTS_PER_RUN]:
        msg = (
            f"⚠️ <b>ALERTA (anomalia)</b>\n"
            f"<b>{escape_html(x['title'])}</b>\n\n"
            f"{escape_html(x['resumo_pt'])}\n\n"
            f"📡 <i>{escape_html(x['source'])}</i> | Score: {x['score']:.1f}\n"
            f"🔗 {escape_html(x['link'])}"
        )
        telegram_send(msg, silent=False)
        stats["alerts"] += 1

    # ===== Novidades do dia (incremental) =====
    sent_today = state.get("sent_updates", {}).get(today_brt, {})
    candidates = [x for x in new_items if x["score"] >= MIN_SCORE_SEND]

    to_send = []
    for x in candidates:
        if sent_today.get(x["fp"]) == True:
            continue
        to_send.append(x)

    for x in to_send[:MAX_UPDATES_PER_RUN]:
        msg = (
            f"🛰️ <b>NOVIDADE (hoje)</b>\n"
            f"<b>{escape_html(x['title'])}</b>\n\n"
            f"{escape_html(x['resumo_pt'])}\n\n"
            f"📡 <i>{escape_html(x['source'])}</i> | Score: {x['score']:.1f}\n"
            f"🔗 {escape_html(x['link'])}"
        )
        telegram_send(msg, silent=True)
        stats["updates_sent"] += 1

        state.setdefault("sent_updates", {})
        state["sent_updates"].setdefault(today_brt, {})
        state["sent_updates"][today_brt][x["fp"]] = True

    # ===== Enfileira para digest =====
    for x in new_items:
        if x["score"] >= MIN_SCORE_DIGEST:
            state["digest_queue"].append({**x, "queued_ts": int(time.time())})

    # ===== Digest (1x por manhã/noite) =====
    slot = in_digest_window()
    if slot and should_send_digest(state, slot):
        dt = now_brt()
        period = "🌅 MANHÃ" if slot == "morning" else "🌙 NOITE"

        queue = [x for x in state["digest_queue"] if x.get("day_brt") == today_brt]
        queue.sort(key=lambda x: x["score"], reverse=True)
        top = queue[:12]

        if top:
            lines = [
                f"🛰️ <b>Digest Lançamentos & Setor</b> - {period}",
                f"📅 {dt.strftime('%d/%m/%Y às %H:%M')} (BRT)\n"
            ]
            for i, x in enumerate(top, 1):
                lines.append(
                    f"{i}) <b>{escape_html(x['title'])}</b>\n"
                    f"📡 <i>{escape_html(x['source'])}</i> | {x['score']:.1f}\n"
                    f"🔗 {escape_html(x['link'])}\n"
                )
            telegram_send("\n".join(lines), silent=True)

        mark_digest_sent(state, slot)
        state["digest_queue"] = [x for x in state["digest_queue"] if x.get("day_brt") != today_brt]

    # ===== Enxuga histórico =====
    if len(state["seen"]) > 8000:
        items = list(state["seen"].items())
        items.sort(key=lambda kv: kv[1].get("ts", 0), reverse=True)
        state["seen"] = dict(items[:6000])

    # limpeza de sent_updates antigos
    try:
        keys = sorted(state.get("sent_updates", {}).keys())
        if len(keys) > 10:
            for k in keys[:-7]:
                state["sent_updates"].pop(k, None)
    except Exception:
        pass

    save_json(STATE_PATH, state)

    print(f"Done. New(today) items: {len(new_items)} | BRT: {now_brt().strftime('%H:%M')}")
    print("[STATS]", json.dumps(stats, ensure_ascii=False))

    # Debug no Telegram
    if DEBUG_TELEGRAM:
        msg = (
            f"🔧 <b>Debug Agente</b>\n"
            f"📅 {now_brt().strftime('%d/%m %H:%M')} BRT\n\n"
            f"<code>{escape_html(json.dumps(stats, ensure_ascii=False, indent=1))}</code>\n\n"
            f"New items: {len(new_items)}"
        )
        telegram_send(msg, silent=True)

if __name__ == "__main__":
    main()
