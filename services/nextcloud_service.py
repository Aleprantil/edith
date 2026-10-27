import logging
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

import caldav
import requests
from dateutil import tz as dateutil_tz

from config import CALDAV_URL, CALDAV_USER, CALDAV_PASSWORD

logger = logging.getLogger(__name__)

TZ_ROMA = ZoneInfo("Europe/Rome")
TZ_UTC = ZoneInfo("UTC")

HTTP_TIMEOUT = 15            # secondi, per ogni singola richiesta HTTP
CALENDARS_TTL = 600          # secondi di validità della lista calendari in cache
# Calendari che non contengono eventi (es. le card di Nextcloud Deck sono task)
IGNORED_CALENDAR_PREFIXES = ("deck:",)

_client = None
_calendars = None
_calendars_ts = 0.0
_lock = threading.RLock()
_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="caldav")


class CalendarError(Exception):
    """Nextcloud non raggiungibile o operazione non eseguibile."""


# ---------------------------------------------------------------------------
# Client e cache dei calendari
# ---------------------------------------------------------------------------

def _get_client():
    """Un solo client riusato: mantiene la connessione HTTPS aperta (keep-alive)."""
    global _client
    if _client is None:
        _client = caldav.DAVClient(
            url=CALDAV_URL,
            username=CALDAV_USER,
            password=CALDAV_PASSWORD,
            timeout=HTTP_TIMEOUT,
        )
    return _client


def _explain_failure(exc: Exception) -> str:
    """
    Spiega in italiano PERCHÉ Nextcloud non ha funzionato: rifà una richiesta semplice e guarda cosa risponde.
    (Un errore come «'NoneType' object has no attribute 'tag'» di caldav, da solo, non dice nulla.)
    """
    try:
        r = requests.request("PROPFIND", CALDAV_URL, auth=(CALDAV_USER, CALDAV_PASSWORD),
                             headers={"Depth": "0"}, timeout=8, allow_redirects=False)
    except requests.exceptions.SSLError:
        return "certificato HTTPS di Nextcloud non valido"
    except requests.exceptions.RequestException as e:
        return f"Nextcloud non raggiungibile ({e.__class__.__name__}): controlla rete, Tailscale e /etc/hosts"

    code = r.status_code
    ctype = (r.headers.get("content-type") or "").split(";")[0].strip() or "n/d"
    size = len(r.content)
    if 300 <= code < 400:
        where = r.headers.get("location") or "?"
        return (f"Nextcloud reindirizza le richieste del bot a «{where}» (HTTP {code}): di solito vuol dire credenziali "
                f"non accettate (crea una nuova password per app e aggiorna CALDAV_PASSWORD) oppure CALDAV_URL sbagliato "
                f"(http/https o dominio diverso)")
    if code in (401, 403):
        return f"Nextcloud rifiuta le credenziali (HTTP {code}): controlla CALDAV_USER/CALDAV_PASSWORD o la password per app"
    if code == 404:
        return "indirizzo CalDAV non trovato (HTTP 404): controlla CALDAV_URL"
    if code == 503:
        return "Nextcloud è in manutenzione o sovraccarico (HTTP 503)"
    if code >= 500:
        return f"errore interno di Nextcloud (HTTP {code}): guarda i suoi log"
    if code in (200, 207) and size == 0:
        return (f"Nextcloud risponde vuoto (HTTP {code}, 0 byte): probabile errore interno di PHP, del web server "
                f"o del proxy; guarda i log di Nextcloud e controlla lo spazio su disco")
    if code in (200, 207) and "xml" not in ctype:
        m = re.search(r"<title>\s*(.*?)\s*</title>", r.text[:4000], re.S | re.I)
        titolo = f" («{m.group(1)[:40]}»)" if m and m.group(1) else ""
        return f"Nextcloud risponde con una pagina web{titolo} invece di WebDAV: controlla CALDAV_URL, le credenziali e il proxy"
    return (f"risposta inattesa da Nextcloud ({exc.__class__.__name__}: {str(exc)[:80]}); "
            f"una richiesta semplice però risponde (HTTP {code}): può essere un guasto passeggero")


def health_check() -> tuple:
    """(ok, spiegazione, millisecondi): una richiesta WebDAV leggera, per sapere se Nextcloud risponde davvero."""
    t0 = time.perf_counter()
    try:
        r = requests.request("PROPFIND", CALDAV_URL, auth=(CALDAV_USER, CALDAV_PASSWORD),
                             headers={"Depth": "0"}, timeout=8, allow_redirects=False)
    except requests.exceptions.RequestException as e:
        return False, _explain_failure(e), None
    ms = int((time.perf_counter() - t0) * 1000)
    ctype = (r.headers.get("content-type") or "").lower()
    if r.status_code == 207 and "xml" in ctype and r.content:
        return True, "", ms
    return False, _explain_failure(RuntimeError(f"HTTP {r.status_code}")), ms


def invalidate_cache():
    global _client, _calendars
    with _lock:
        _client = None
        _calendars = None


def get_calendars(force: bool = False) -> list:
    global _calendars, _calendars_ts
    with _lock:
        fresh = _calendars is not None and (time.time() - _calendars_ts) < CALENDARS_TTL
        if fresh and not force:
            return _calendars

        t0 = time.perf_counter()
        try:
            cals = _get_client().principal().calendars()
        except Exception as e:
            invalidate_cache()
            why = _explain_failure(e)
            logger.warning("Calendari non disponibili: %s [%s: %s]", why, e.__class__.__name__, str(e)[:120])
            raise CalendarError(why) from e

        cals = [
            c for c in cals
            if c.name and not c.name.lower().startswith(IGNORED_CALENDAR_PREFIXES)
        ]
        _calendars, _calendars_ts = cals, time.time()
        logger.info("[timing] lista calendari (%d) caricata in %.1fs", len(cals), time.perf_counter() - t0)
        return cals


def prewarm():
    """Scalda cache e connessione; non solleva mai eccezioni."""
    try:
        get_calendars()
    except CalendarError as e:
        logger.warning("Prewarm calendari fallito: %s", e)


# ---------------------------------------------------------------------------
# Utilità
# ---------------------------------------------------------------------------

def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=TZ_ROMA)


def _to_rome(value):
    if isinstance(value, datetime):
        return value.astimezone(TZ_ROMA) if value.tzinfo else value.replace(tzinfo=TZ_ROMA)
    return value  # date (evento tutto il giorno) oppure None


def _sort_key(ev: dict) -> datetime:
    s = ev.get("start")
    if isinstance(s, datetime):
        return _aware(s)
    if isinstance(s, date):
        return datetime.combine(s, dtime.min, tzinfo=TZ_ROMA)
    return datetime(1970, 1, 1, tzinfo=TZ_ROMA)


def _to_vobject_utc(dt: datetime) -> datetime:
    """
    vobject non sa scrivere i fusi ZoneInfo: inventa un fuso fisso "CET" (+1, senza ora legale)
    e con l'ora legale l'evento finisce con un'ora di scarto. In UTC con tzutc di dateutil
    l'orario viene scritto come ...Z ed è sempre corretto.
    """
    return dt.astimezone(dateutil_tz.tzutc())


def _ics_escape(text: str) -> str:
    return (
        (text or "")
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _event_to_dict(ev, cal_name: str):
    try:
        vevent = ev.vobject_instance.vevent
    except Exception:
        return None

    return {
        "title": str(vevent.summary.value) if hasattr(vevent, "summary") else "Senza titolo",
        "start": _to_rome(vevent.dtstart.value) if hasattr(vevent, "dtstart") else None,
        "end": _to_rome(vevent.dtend.value) if hasattr(vevent, "dtend") else None,
        "calendar": cal_name,
        "location": str(vevent.location.value) if hasattr(vevent, "location") else "",
        "recurring": hasattr(vevent, "rrule") or hasattr(vevent, "recurrence_id"),
    }


def _pick_calendar(calendars: list, name: str):
    name = (name or "generale").lower()
    synonyms = {
        "allenamento": "calcio",
        "allenamento bambini": "calcio bambini",
        "partita": "calcio",
        "calcetto": "calcio",
    }
    term = synonyms.get(name, name)
    for cal in calendars:            # prima il nome esatto...
        if cal.name.lower() == term:
            return cal
    for cal in calendars:            # ...poi quello che lo contiene
        if term in cal.name.lower():
            return cal
    for cal in calendars:            # nessuna corrispondenza: uso "svago", se esiste
        if cal.name.lower() == "svago":
            return cal
    return calendars[0]


def _search_one(cal, start_utc: datetime, end_utc: datetime):
    try:
        return cal.search(start=start_utc, end=end_utc, event=True, expand=True)
    except Exception:
        return cal.search(start=start_utc, end=end_utc, event=True)


def _search_all(start_dt: datetime, end_dt: datetime) -> list:
    """Cerca in PARALLELO su tutti i calendari. Ritorna [(calendar, event), ...]."""
    calendars = get_calendars()
    start_utc = _aware(start_dt).astimezone(TZ_UTC)
    end_utc = _aware(end_dt).astimezone(TZ_UTC)

    def work(cal):
        try:
            return cal, _search_one(cal, start_utc, end_utc), None
        except Exception as e:  # un calendario "rotto" non deve bloccare gli altri
            return cal, [], e

    t0 = time.perf_counter()
    results = list(_pool.map(work, calendars))

    failed = [(cal, err) for cal, _, err in results if err is not None]
    for cal, err in failed:
        logger.warning("Ricerca fallita nel calendario '%s': %s", cal.name, err)
    if calendars and len(failed) == len(results):
        invalidate_cache()
        raise CalendarError(_explain_failure(failed[0][1]))

    pairs = [(cal, ev) for cal, evs, _ in results for ev in evs]
    logger.info(
        "[timing] ricerca su %d calendari: %.1fs, %d eventi",
        len(calendars), time.perf_counter() - t0, len(pairs),
    )
    return pairs


# ---------------------------------------------------------------------------
# Operazioni
# ---------------------------------------------------------------------------

def create_calendar_event(
    title: str, start_time_str: str, description: str = "",
    calendar_name: str = "generale", rrule: str = None,
    end_time_str: str = None, location: str = ""
) -> dict:
    try:
        calendars = get_calendars()
        if not calendars:
            return {"success": False, "reason": "error", "message": "Nessun calendario trovato."}

        target = _pick_calendar(calendars, calendar_name)

        start_dt = _aware(datetime.fromisoformat(start_time_str))
        end_dt = _aware(datetime.fromisoformat(end_time_str)) if end_time_str else start_dt + timedelta(hours=1)
        if end_dt <= start_dt:
            end_dt = start_dt + timedelta(hours=1)

        start_utc_dt = start_dt.astimezone(TZ_UTC)
        end_utc_dt = end_dt.astimezone(TZ_UTC)

        # Controllo conflitti (solo nel calendario di destinazione). Se fallisce non blocca il salvataggio.
        try:
            has_conflict = bool(target.search(start=start_utc_dt, end=end_utc_dt, event=True))
        except Exception as e:
            logger.warning("Controllo conflitti fallito: %s", e)
            has_conflict = False

        start_utc = start_utc_dt.strftime("%Y%m%dT%H%M%SZ")
        end_utc = end_utc_dt.strftime("%Y%m%dT%H%M%SZ")
        uid = str(uuid.uuid4())
        dtstamp = datetime.now(TZ_UTC).strftime("%Y%m%dT%H%M%SZ")

        clean_rrule = (rrule or "").replace("RRULE:", "").strip()
        rrule_line = f"RRULE:{clean_rrule}\n" if clean_rrule else ""
        loc = (location or "").strip()
        location_line = f"LOCATION:{_ics_escape(loc)}\n" if loc else ""

        event_ics = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//OpenJarvis Bot//IT
BEGIN:VEVENT
UID:{uid}
DTSTAMP:{dtstamp}
SUMMARY:{_ics_escape(title)}
DESCRIPTION:{_ics_escape(description)}
{location_line}DTSTART:{start_utc}
DTEND:{end_utc}
{rrule_line}END:VEVENT
END:VCALENDAR"""

        target.save_event(event_ics)
        return {
            "success": True,
            "calendar_used": target.name,
            "conflict": has_conflict,
            "location": loc,
        }
    except Exception as e:
        logger.exception("Errore create_calendar_event")
        if not isinstance(e, CalendarError):
            invalidate_cache()
        return {"success": False, "reason": "error", "message": str(e)[:300]}


def get_calendar_events(start_dt: datetime, end_dt: datetime) -> list:
    """Eventi in [start_dt, end_dt] da tutti i calendari, ordinati. Solleva CalendarError se Nextcloud non risponde."""
    events = []
    for cal, ev in _search_all(start_dt, end_dt):
        d = _event_to_dict(ev, cal.name)
        if d:
            events.append(d)
    events.sort(key=_sort_key)
    return events


def _refuse_recurring(title: str, action: str):
    raise CalendarError(
        f"'{title}' è un evento ricorrente: per non {action} l'intera serie "
        f"preferisco non toccarlo. Gestisci la singola occorrenza da Nextcloud."
    )


def delete_calendar_event(calendar_name: str, search_dt: datetime, title: str, days: int = 1):
    """
    Cancella il primo evento il cui titolo contiene `title` nella finestra di `days` giorni
    a partire dal giorno di `search_dt`. Ritorna il dict dell'evento cancellato, oppure None.
    """
    target_title = (title or "").strip().lower()
    if not target_title:
        return None

    search_dt = _aware(search_dt)
    day_start = datetime.combine(search_dt.date(), dtime.min, tzinfo=TZ_ROMA)
    day_end = day_start + timedelta(days=days)

    safe_name = (calendar_name or "").lower()
    candidates = []
    for cal, ev in _search_all(day_start, day_end):
        d = _event_to_dict(ev, cal.name)
        if not d or target_title not in d["title"].strip().lower():
            continue
        prio = 0 if safe_name and safe_name in cal.name.lower() else 1
        candidates.append((_sort_key(d).date(), prio, _sort_key(d), ev, d))

    if not candidates:
        return None

    candidates.sort(key=lambda c: c[:3])
    _, _, _, ev, d = candidates[0]

    if d["recurring"]:
        _refuse_recurring(d["title"], "cancellare")

    try:
        ev.delete()
    except Exception as e:
        invalidate_cache()
        raise CalendarError(f"Cancellazione fallita ({e.__class__.__name__})") from e

    logger.info("[OK] Evento '%s' cancellato dal calendario '%s'", d["title"], d["calendar"])
    return d


def update_calendar_event(
    calendar_name: str,
    original_dt: datetime,
    new_start: datetime,
    new_end: datetime = None,
    title: str = ""
) -> bool:
    """Cerca l'evento nel giorno indicato e lo sposta al nuovo orario."""
    original_dt = _aware(original_dt)
    new_start = _aware(new_start)
    if new_end is not None:
        new_end = _aware(new_end)

    day_start = datetime.combine(original_dt.date(), dtime.min, tzinfo=TZ_ROMA)
    day_end = day_start + timedelta(days=1)

    safe_name = (calendar_name or "").lower()
    wanted = (title or "").strip().lower()

    pairs = _search_all(day_start, day_end)
    pairs.sort(key=lambda p: 0 if safe_name and safe_name in p[0].name.lower() else 1)

    for cal, ev in pairs:
        try:
            vevent = ev.vobject_instance.vevent
        except Exception:
            continue

        summary = str(vevent.summary.value).strip().lower() if hasattr(vevent, "summary") else ""
        if wanted and wanted not in summary:
            continue

        if hasattr(vevent, "rrule") or hasattr(vevent, "recurrence_id"):
            _refuse_recurring(summary or title, "spostare")

        try:
            if not isinstance(vevent.dtstart.value, datetime):   # era "tutto il giorno"
                raise ValueError
            durata = vevent.dtend.value - vevent.dtstart.value
        except Exception:
            durata = timedelta(hours=1)

        end = new_end or (new_start + durata)

        try:
            # Ricreo le righe invece di modificarle: così non restano parametri del vecchio
            # fuso (TZID) né VALUE=DATE di un evento "tutto il giorno".
            for name, value in (("dtstart", new_start), ("dtend", end)):
                vevent.contents.pop(name, None)
                vevent.add(name).value = _to_vobject_utc(value)
            ev.save()
        except Exception as e:
            invalidate_cache()
            raise CalendarError(f"Aggiornamento fallito ({e.__class__.__name__})") from e

        logger.info("[OK] Evento '%s' spostato al %s nel calendario %s", summary, new_start, cal.name)
        return True

    return False


def find_event(calendar_name: str, search_dt: datetime, title: str, days: int = 1, action: str = "modificare"):
    """
    Cerca, SENZA modificare nulla, l'evento il cui titolo contiene `title` nella finestra di `days` giorni
    dal giorno di `search_dt`. Ritorna (dict dell'evento, oggetto evento) oppure None.
    Serve a chiedere conferma prima di cancellare o spostare. Rifiuta gli eventi ricorrenti (CalendarError).
    """
    target_title = (title or "").strip().lower()
    if not target_title:
        return None

    search_dt = _aware(search_dt)
    day_start = datetime.combine(search_dt.date(), dtime.min, tzinfo=TZ_ROMA)
    day_end = day_start + timedelta(days=days)

    safe_name = (calendar_name or "").lower()
    candidates = []
    for cal, ev in _search_all(day_start, day_end):
        d = _event_to_dict(ev, cal.name)
        if not d or target_title not in d["title"].strip().lower():
            continue
        prio = 0 if safe_name and safe_name in cal.name.lower() else 1
        candidates.append((_sort_key(d).date(), prio, _sort_key(d), ev, d))

    if not candidates:
        return None
    candidates.sort(key=lambda c: c[:3])
    _, _, _, ev, d = candidates[0]
    if d["recurring"]:
        _refuse_recurring(d["title"], action)
    return d, ev


def delete_found(ev, d: dict) -> None:
    """Cancella un evento già trovato con find_event."""
    try:
        ev.delete()
    except Exception as e:
        invalidate_cache()
        raise CalendarError(f"Cancellazione fallita ({e.__class__.__name__})") from e
    logger.info("[OK] Evento '%s' cancellato dal calendario '%s'", d.get("title"), d.get("calendar"))


def update_found(ev, d: dict, new_start: datetime, new_end: datetime = None) -> None:
    """Sposta un evento già trovato con find_event (mantiene la durata se non indichi la fine)."""
    new_start = _aware(new_start)
    if new_end is not None:
        new_end = _aware(new_end)
    try:
        vevent = ev.vobject_instance.vevent
        try:
            if not isinstance(vevent.dtstart.value, datetime):   # era "tutto il giorno"
                raise ValueError
            durata = vevent.dtend.value - vevent.dtstart.value
        except Exception:
            durata = timedelta(hours=1)
        end = new_end or (new_start + durata)
        for name, value in (("dtstart", new_start), ("dtend", end)):
            vevent.contents.pop(name, None)
            vevent.add(name).value = _to_vobject_utc(value)
        ev.save()
    except Exception as e:
        invalidate_cache()
        raise CalendarError(f"Aggiornamento fallito ({e.__class__.__name__})") from e
    logger.info("[OK] Evento '%s' spostato al %s", d.get("title"), new_start)


def get_free_slots(search_date: datetime, calendar_name: str = "lavoro") -> list:
    """Trova i buchi liberi tra le 07:00 e le 21:00 del giorno indicato."""
    search_date = _aware(search_date)
    start_of_day = search_date.replace(hour=7, minute=0, second=0, microsecond=0)
    end_of_day = search_date.replace(hour=21, minute=0, second=0, microsecond=0)

    events = get_calendar_events(start_of_day, end_of_day)

    free_slots = []
    current_time = start_of_day

    for ev in events:
        ev_start, ev_end = ev.get("start"), ev.get("end")
        # Solo impegni con orario preciso (esclusi i "tutto il giorno")
        if isinstance(ev_start, datetime) and isinstance(ev_end, datetime):
            if ev_start > current_time:
                free_slots.append((current_time, ev_start))
            current_time = max(current_time, ev_end)

    if current_time < end_of_day:
        free_slots.append((current_time, end_of_day))

    return free_slots
