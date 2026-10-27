"""
Dati per le schede della pagina. Ogni scheda è indipendente: se una fallisce, le altre funzionano.
Le funzioni riusano i servizi del bot e ritornano solo dizionari semplici (JSON).
"""
import logging
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

from services import digest, monitor_service, news_service, nextcloud_service, notes_service, plugins, tasks_service, weather_service
from services.assistant import sanitize_html

logger = logging.getLogger(__name__)
TZ = ZoneInfo("Europe/Rome")
OWNER_ID = 0            # impostato da server.py
UPCOMING_DAYS = 4


class CardUnavailable(Exception):
    """La scheda non è disponibile su questo server (es. finanze non configurate)."""


# ---------------------------------------------------------------------------
# Piccola cache, per non rifare le stesse richieste a ogni apertura della pagina
# ---------------------------------------------------------------------------
_cache = {}
_locks = defaultdict(threading.Lock)


def cached(name, ttl, fn, refresh=False):
    with _locks[name]:
        hit = _cache.get(name)
        if hit and not refresh and time.time() - hit[0] < ttl:
            return hit[1]
        data = fn()
        _cache[name] = (time.time(), data)
        return data


def clear_cache():
    _cache.clear()


def invalidate(name: str):
    _cache.pop(name, None)


# ---------------------------------------------------------------------------
# Oggi
# ---------------------------------------------------------------------------

def greeting(now: datetime) -> str:
    h = now.hour
    return "Buonanotte" if h < 5 else "Buongiorno" if h < 12 else "Buon pomeriggio" if h < 18 else "Buonasera"


def day_label(d: date) -> str:
    return f"{digest.GIORNI[d.weekday()]} {d.day} {digest.MESI[d.month - 1]}"


def _event_json(e: dict, now: datetime) -> dict:
    st, en = e.get("start"), e.get("end")
    base = {"title": e.get("title") or "Senza titolo", "calendar": e.get("calendar") or "", "location": e.get("location") or ""}
    if isinstance(st, datetime):
        end_dt = en if isinstance(en, datetime) else st + timedelta(hours=1)
        return {**base, "all_day": False, "time": st.strftime("%H:%M"),
                "end": en.strftime("%H:%M") if isinstance(en, datetime) and en.date() == st.date() else "",
                "past": end_dt <= now, "now": st <= now < end_dt,
                "start_iso": st.isoformat(), "end_iso": end_dt.isoformat()}              # per il «prossimo impegno» con il conto alla rovescia
    return {**base, "all_day": True, "time": "", "end": "", "past": False, "now": False, "start_iso": None, "end_iso": None}


_GIORNI_BREVI = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]


def _weather_days(days: list, today: date) -> list:
    """Fino a tre giorni, già pronti da mostrare: «oggi», «domani», poi il giorno della settimana."""
    out = []
    for i, d in enumerate(days[:3]):
        try:
            when = date.fromisoformat(str(d.get("date"))[:10])
            label = "oggi" if when == today else "domani" if when == today + timedelta(days=1) else _GIORNI_BREVI[when.weekday()]
        except ValueError:
            label = ("oggi", "domani", "dopo")[i]
        out.append({"label": label, "tmin": d["tmin"], "tmax": d["tmax"], "emoji": d["emoji"], "desc": d["label"], "pop": d.get("pop")})
    return out


def _weather_json(day: dict):
    if not day:
        return None
    return {"tmin": day["tmin"], "tmax": day["tmax"], "label": day["label"], "emoji": day["emoji"], "pop": day.get("pop")}


def card_today(now: datetime = None) -> dict:
    now = now or datetime.now(TZ)
    today = now.date()
    start = datetime.combine(today, dtime.min, tzinfo=TZ)
    out = {"greeting": greeting(now), "date_label": day_label(today), "events": [], "upcoming": [], "windows": [],
           "summary": "", "weather": None, "weather_tomorrow": None, "weather_days": [], "sun": None, "errors": []}

    try:
        events = nextcloud_service.get_calendar_events(start, start + timedelta(days=UPCOMING_DAYS)) if plugins.on("calendar") else []
    except Exception as e:                           # il calendario non risponde: mostro comunque il resto
        events = None
        out["errors"].append(f"Calendario: {e}")

    if events is not None:
        def on_day(ev, d):
            s = ev.get("start")
            return (s.date() if isinstance(s, datetime) else s) == d

        todays = [ev for ev in events if on_day(ev, today)]
        out["events"] = [_event_json(ev, now) for ev in todays]
        out["summary"] = digest.summary_line(todays, "Oggi")
        out["windows"] = [{"from": a.strftime("%H:%M"), "to": b.strftime("%H:%M"), "minutes": int((b - a).total_seconds() / 60)}
                          for a, b in digest.free_windows(todays, today, TZ, from_dt=now)][:3]
        for i in range(1, UPCOMING_DAYS):
            d = today + timedelta(days=i)
            evs = [ev for ev in events if on_day(ev, d)]
            if evs:
                out["upcoming"].append({"label": "Domani" if i == 1 else day_label(d), "events": [_event_json(ev, now) for ev in evs][:5]})

    try:
        days = weather_service.get_forecast() if plugins.on("weather") else []
        out["weather"] = _weather_json(days[0] if days else None)
        out["weather_tomorrow"] = _weather_json(days[1] if len(days) > 1 else None)
        out["weather_days"] = _weather_days(days, today)
        if days and (days[0].get("sunrise") or days[0].get("sunset")):
            out["sun"] = {"rise": days[0].get("sunrise"), "set": days[0].get("sunset")}
    except Exception as e:
        out["errors"].append(f"Meteo: {e}")
    return out


# ---------------------------------------------------------------------------
# Promemoria
# ---------------------------------------------------------------------------
def card_tasks(now: datetime = None) -> dict:
    """I 7 promemoria più urgenti della lista TO DO (scaduti, di oggi, dei prossimi giorni, più avanti, senza scadenza), raggruppati per lista."""
    now = now or datetime.now(TZ)
    pub = tasks_service.public(tasks_service.open_tasks(), now)
    items, total = tasks_service.top(pub)
    lists = {}
    for t in items:
        lists.setdefault(t["list"], []).append(t)
    return {"lists": [{"name": n, "items": it} for n, it in lists.items()], "total": total, "shown": len(items),
            "hidden": total - len(items), "overdue": sum(1 for t in pub if t["level"] == "overdue")}


# ---------------------------------------------------------------------------
# Note
# ---------------------------------------------------------------------------

def _note_preview(content: str, limit: int = 140) -> str:
    lines = []
    for line in (content or "").splitlines():
        s = line.strip()
        if not s or s.startswith("# ") or s.lower().startswith("*creata il"):
            continue
        lines.append(s.lstrip("-*• ").strip())
    text = " · ".join(x for x in lines if x)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def card_notes() -> dict:
    notes = notes_service.list_notes("")
    notes.sort(key=lambda n: n.get("modified") or 0, reverse=True)
    items = []
    for i, n in enumerate(notes[:6]):
        preview = ""
        if i < 3:                                    # l'anteprima solo per le prime: una richiesta ciascuna
            try:
                preview = _note_preview(notes_service.get_note_content(n["title"], "") or "")
            except Exception:
                preview = ""
        items.append({"title": n["title"], "modified": n.get("modified"), "preview": preview})
    return {"items": items, "total": len(notes)}


# ---------------------------------------------------------------------------
# Finanze
# ---------------------------------------------------------------------------

def _month_start(d: date, k: int) -> date:
    """Primo giorno del mese a k mesi da d (k negativo = nel passato)."""
    m = d.year * 12 + (d.month - 1) + k
    return date(m // 12, m % 12 + 1, 1)


def _finance_series(db, uid: int, today: date) -> dict:
    """Serie per i grafici: spese giorno per giorno (questo mese e il mese scorso) e totali degli ultimi 6 mesi."""
    first = _month_start(today, -5)
    exp = {r["day"]: r["total"] for r in db.get_daily_totals(uid, str(first), str(today), "spesa")}
    inc = {r["day"]: r["total"] for r in db.get_daily_totals(uid, str(first), str(today), "entrata")}
    months = []
    for k in range(-5, 1):
        ms = _month_start(today, k)
        ym = ms.strftime("%Y-%m")
        months.append({"ym": ym, "label": digest.MESI[ms.month - 1][:3],
                       "expense": round(sum(v for d, v in exp.items() if d.startswith(ym)), 2),
                       "income": round(sum(v for d, v in inc.items() if d.startswith(ym)), 2)})
    this, prev = _month_start(today, 0), _month_start(today, -1)
    daily = [round(exp.get(f"{this:%Y-%m}-{d:02d}", 0.0), 2) for d in range(1, today.day + 1)]
    daily_prev = [round(exp.get(f"{prev:%Y-%m}-{d:02d}", 0.0), 2) for d in range(1, (this - prev).days + 1)]
    return {"months": months, "daily": daily, "daily_prev": daily_prev, "month_days": (_month_start(today, 1) - this).days}


def card_finance(today: date = None) -> dict:
    try:
        import constants as C
        import database as db
    except ImportError:
        raise CardUnavailable("Le finanze non sono configurate su questo server.")
    today = today or date.today()
    start = today.replace(day=1)
    uid = OWNER_ID
    spese = db.get_category_totals(uid, str(start), str(today), "spesa")
    entrate = db.get_category_totals(uid, str(start), str(today), "entrata")
    tot_s, tot_e = sum(r["total"] for r in spese), sum(r["total"] for r in entrate)
    spent = {r["category"]: r["total"] for r in spese}

    categories = [{"label": C.categoria_display(r["category"]), "total": r["total"],
                   "pct": (100.0 * r["total"] / tot_s) if tot_s else 0.0} for r in spese[:5]]
    last = []
    for r in db.get_transactions(uid, str(start), str(today))[:6]:
        label = C.categoria_display(r["category"]) if r["type"] == "spesa" else C.entrata_display(r["category"])
        last.append({"date": (r["created_at"] or "")[:10], "type": r["type"], "label": label, "amount": r["amount"], "note": r["note"] or ""})
    budgets = []
    for b in db.get_budgets(uid):
        limit = b["monthly_limit"]
        sp = spent.get(b["category"], 0.0)
        budgets.append({"label": C.categoria_display(b["category"]), "spent": sp, "limit": limit,
                        "pct": (100.0 * sp / limit) if limit else 0.0})
    budgets.sort(key=lambda x: -x["pct"])
    try:
        series = _finance_series(db, uid, today)
    except Exception:                       # i grafici sono un di più: se mancano i dati la scheda funziona lo stesso
        logger.exception("Finanze: serie per i grafici non disponibili")
        series = None
    return {"month_label": f"{digest.MESI[today.month - 1].capitalize()} {today.year}", "income": tot_e, "expense": tot_s,
            "balance": tot_e - tot_s, "categories": categories, "last": last, "budgets": budgets[:4], "currency": "EUR", "series": series}


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------

def card_news() -> dict:
    now = time.time()
    groups = news_service.collect_digest(include_sent=True, max_age_hours=None, per_folder=3)
    out = []
    for g in sorted(groups, key=digest._group_rank):
        items = []
        for it in g["items"]:
            url = it.get("url") or ""
            items.append({"title": it.get("line") or it["title"], "url": url if url.startswith(("http://", "https://")) else "",
                          "source": it.get("source") or "", "age": digest._ago(now - it["pub"]) if it.get("pub") else ""})
        out.append({"title": digest.group_title(g), "items": items})
    return {"groups": out}


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

def card_server() -> dict:
    data = monitor_service.get_monitor().card()
    data["alerts"] = [{"level": a["level"], "html": sanitize_html(a["text"])} for a in data.get("alerts", [])]
    return data


CARDS = {
    "today": (card_today, 20),
    "tasks": (card_tasks, 20),
    "finance": (card_finance, 20),
    "notes": (card_notes, 60),
    "news": (card_news, 900),
    "server": (card_server, 5),
}
ORDER = ["today", "tasks", "finance", "notes", "news", "server"]


def active_order() -> list:
    return [n for n in ORDER if plugins.card_on(n)]


def get_card(name: str, refresh: bool = False) -> dict:
    fn, ttl = CARDS[name]                       # KeyError se la scheda non esiste
    if not plugins.card_on(name):
        raise CardUnavailable(f"Il modulo della scheda «{name}» non è attivo")
    return cached(name, ttl, fn, refresh)
