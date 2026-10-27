"""
Promemoria (app «Tasks» di Nextcloud).

Sono voci VTODO dei calendari CalDAV: lo stesso sistema degli eventi, quindi usiamo lo stesso collegamento.
Le liste sono i calendari che accettano i VTODO (la tua si chiama «TO DO»).

Regole sulle scadenze (decise da te, applicate qui nel codice e non lasciate a Gemini):
  - «entro le 12» (solo l'ora)        -> oggi alle 12:00 (se è già passata: domani alle 12:00, e lo dico)
  - «domani» / «entro domani» (giorno) -> quel giorno alle 23:59
  - giorno e ora                       -> quel giorno a quell'ora
  - né giorno né ora                   -> senza scadenza
"""
import logging
import re
import threading
import time
import uuid
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from dateutil import tz as dutz

from config import TASKS_DEFAULT_LIST, TASKS_LISTS
from services import nextcloud_service as nc

logger = logging.getLogger(__name__)

TZ_ROMA = ZoneInfo("Europe/Rome")
END_OF_DAY = dtime(23, 59)
LISTS_TTL = 300
SHOWN = 7                                          # quanti promemoria si mostrano (scheda, agenda, buongiorno), dal più urgente
_GIORNI = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
_MESI = ["gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic"]

_lock = threading.Lock()
_lists_cache = {"at": 0.0, "cals": None, "all": []}


class TaskError(Exception):
    """Errore comprensibile (lista non trovata, Nextcloud che non risponde…): il messaggio si può mostrare così com'è."""


# ---------------------------------------------------------------------------
# Scadenze
# ---------------------------------------------------------------------------

def _parse_day(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).strip()[:10]).date() if len(str(value).strip()) >= 10 else None
    except ValueError:
        return None


def _parse_clock(value):
    if not value:
        return None
    m = re.fullmatch(r"\s*(\d{1,2})(?:[:.](\d{2}))?(?::\d{2})?\s*", str(value))
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    return dtime(h, mi) if 0 <= h <= 23 and 0 <= mi <= 59 else None


def compute_due(now: datetime, day, clock):
    """(scadenza o None, nota). `day` è «YYYY-MM-DD» o None, `clock` è «HH:MM» o None."""
    d, t = _parse_day(day), _parse_clock(clock)
    if d is None and t is None:
        return None, ""
    if t is None:
        return datetime.combine(d, END_OF_DAY, tzinfo=TZ_ROMA), ""
    if d is not None:
        return datetime.combine(d, t, tzinfo=TZ_ROMA), ""
    due = datetime.combine(now.date(), t, tzinfo=TZ_ROMA)
    if due <= now:                                   # solo l'ora, e oggi è già passata: la prossima volta che capita
        return due + timedelta(days=1), "oggi quell'ora è già passata"
    return due, ""


def _day_word(d: date, now: datetime) -> str:
    delta = (d - now.date()).days
    if delta == 0:
        return "oggi"
    if delta == 1:
        return "domani"
    if delta == -1:
        return "ieri"
    if 1 < delta < 7:
        return _GIORNI[d.weekday()]
    return f"{d.day} {_MESI[d.month - 1]}"


def due_label(due, now: datetime) -> str:
    """«oggi 12:00», «domani», «giovedì», «scaduto ieri», «scaduto da 2 g»; vuoto se non c'è scadenza."""
    if due is None:
        return ""
    end_of_day = (due.hour, due.minute) == (23, 59)
    if due < now and not (end_of_day and due.date() >= now.date()):
        if due.date() == now.date():
            return f"scaduto alle {due:%H:%M}"
        days = (now.date() - due.date()).days
        return "scaduto ieri" if days == 1 else f"scaduto da {days} g"
    word = _day_word(due.date(), now)
    return word if end_of_day else f"{word} {due:%H:%M}"


def due_level(due, now: datetime) -> str:
    """overdue / today / soon (entro 7 giorni) / later / none"""
    if due is None:
        return "none"
    end_of_day = (due.hour, due.minute) == (23, 59)
    if due < now and not (end_of_day and due.date() >= now.date()):
        return "overdue"
    if due.date() == now.date():
        return "today"
    return "soon" if (due.date() - now.date()).days <= 7 else "later"


# ---------------------------------------------------------------------------
# Liste
# ---------------------------------------------------------------------------

def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _calendars():
    try:
        return nc._get_client().principal().calendars()
    except Exception as e:
        nc.invalidate_cache()
        raise TaskError(nc._explain_failure(e)) from e


def task_lists(force: bool = False) -> list:
    """Le liste di promemoria che uso: SOLO quelle indicate in TASKS_LISTS (di serie, «TO DO»). Gli altri calendari si ignorano. Cache di 5 minuti."""
    with _lock:
        if not force and _lists_cache["cals"] is not None and time.time() - _lists_cache["at"] < LISTS_TTL:
            return _lists_cache["cals"]
    found = []
    for c in _calendars():
        try:
            comps = c.get_supported_components()
        except Exception:
            comps = []
        if "VTODO" in [str(x).upper() for x in comps]:
            found.append(c)
    wanted = {_norm(n) for n in TASKS_LISTS}
    out = [c for c in found if _norm(c.name) in wanted]
    with _lock:
        _lists_cache.update(at=time.time(), cals=out, all=[c.name for c in found])
    return out


def _no_list_message() -> str:
    names = ", ".join(f"«{n}»" for n in TASKS_LISTS)
    others = _lists_cache.get("all") or []
    extra = (" Quelle che trovo in Nextcloud: " + ", ".join(f"«{n}»" for n in others) + ".") if others else ""
    return f"Non trovo la lista di promemoria {names} in Nextcloud Tasks.{extra} Se il nome è diverso, impostalo con TASKS_DEFAULT_LIST nel .env."


def pick_list(name=None):
    """La lista indicata («todo» trova «TO DO») tra quelle ammesse; se non indichi nulla, quella predefinita."""
    lists = task_lists()
    if not lists:
        raise TaskError(_no_list_message())
    wanted = _norm(name or TASKS_DEFAULT_LIST)
    for c in lists:
        if _norm(c.name) == wanted:
            return c
    if name:
        for c in lists:
            if wanted and (wanted in _norm(c.name) or (_norm(c.name) and _norm(c.name) in wanted)):
                return c
        raise TaskError(f"Non trovo la lista «{name}»: uso solo " + ", ".join(f"«{c.name}»" for c in lists) + ".")
    return lists[0]                                  # predefinita non trovata (rinominata?): la prima tra quelle ammesse


# ---------------------------------------------------------------------------
# Lettura
# ---------------------------------------------------------------------------

def _val(v, name):
    try:
        return getattr(v, name).value if hasattr(v, name) else None
    except Exception:
        return None


def _to_aware(value):
    """DUE può essere una data, un'ora senza fuso (la leggo come ora italiana) o con fuso."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=TZ_ROMA) if value.tzinfo is None else value.astimezone(TZ_ROMA)
    if isinstance(value, date):
        return datetime.combine(value, END_OF_DAY, tzinfo=TZ_ROMA)
    return None


def _todo_to_dict(obj, list_name: str):
    try:
        v = obj.vobject_instance.vtodo
    except Exception:
        return None
    uid, title = _val(v, "uid"), (_val(v, "summary") or "").strip()
    if not uid:
        return None
    pct = _val(v, "percent_complete")
    status = str(_val(v, "status") or "").upper()
    done = status == "COMPLETED" or str(pct).strip() == "100" or _val(v, "completed") is not None
    return {"uid": str(uid), "title": title or "(senza titolo)", "due": _to_aware(_val(v, "due")), "done": bool(done),
            "list": list_name, "recurring": hasattr(v, "rrule"), "_obj": obj}


def _fetch(cal, include_completed: bool):
    try:
        return cal.todos(include_completed=include_completed)
    except Exception:                                # alcune versioni/server preferiscono la ricerca diretta
        return cal.search(todo=True, include_completed=include_completed)


def open_tasks(list_name=None, include_completed: bool = False) -> list:
    """Promemoria (di una lista o di tutte), con prima quelli scaduti, poi per scadenza, poi quelli senza scadenza."""
    cals = [pick_list(list_name)] if list_name else task_lists()
    if not cals:                                      # lista TO DO non trovata (rinominata?): lo dico, non fingo che sia vuota
        raise TaskError(_no_list_message())
    out = []
    for cal in cals:
        try:
            todos = _fetch(cal, include_completed)
        except Exception as e:
            nc.invalidate_cache()
            raise TaskError(nc._explain_failure(e)) from e
        for t in todos:
            d = _todo_to_dict(t, cal.name)
            if d and (include_completed or not d["done"]):
                out.append(d)
    far = datetime.max.replace(tzinfo=TZ_ROMA)
    out.sort(key=lambda d: (d["due"] is None, d["due"] or far, d["title"].lower()))
    return out


_RANK = {"overdue": 0, "today": 1, "soon": 2, "later": 3, "none": 4}


def top(pub: list, n: int = SHOWN):
    """I n più urgenti (scaduti, di oggi, prossimi giorni, più avanti, senza scadenza) e il totale. `pub` arriva da public()."""
    ordered = sorted(pub, key=lambda t: _RANK.get(t["level"], 5))          # a parità resta l'ordine per scadenza
    return ordered[:n], len(pub)


def public(tasks: list, now: datetime) -> list:
    """Versione senza oggetti interni, pronta per JSON, con le etichette in italiano."""
    return [{"uid": t["uid"], "title": t["title"], "list": t["list"], "due": t["due"].isoformat() if t["due"] else None,
             "label": due_label(t["due"], now), "level": due_level(t["due"], now)} for t in tasks]


# ---------------------------------------------------------------------------
# Ricerca per parole («da pagina 20 a 30»)
# ---------------------------------------------------------------------------

def _tokens(s: str) -> list:
    return re.findall(r"\w+", (s or "").lower())


def match(query: str, tasks: list):
    """(esatti, approssimati): esatti = contengono tutte le parole cercate (numeri compresi); approssimati = ne contengono almeno il 60%."""
    q = [w for w in _tokens(query) if w not in {"il", "lo", "la", "un", "una", "di", "del", "promemoria"}] or _tokens(query)
    if not q:
        return [], []
    exact, fuzzy = [], []
    for t in tasks:
        words = set(_tokens(t["title"]))
        hit = sum(1 for w in q if w in words)
        if hit == len(q):
            exact.append(t)
        elif hit / len(q) >= 0.6:
            fuzzy.append((hit / len(q), t))
    exact.sort(key=lambda t: len(_tokens(t["title"])))        # tra due esatti prima il titolo più corto (più preciso)
    fuzzy.sort(key=lambda x: -x[0])
    return exact, [t for _, t in fuzzy]


# ---------------------------------------------------------------------------
# Scrittura
# ---------------------------------------------------------------------------

def _utc(dt: datetime) -> str:
    return dt.astimezone(dutz.tzutc()).strftime("%Y%m%dT%H%M%SZ")


def build_ical(uid: str, title: str, due, description: str = "", now: datetime = None) -> str:
    now = now or datetime.now(TZ_ROMA)
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//EDITH//Promemoria//IT", "BEGIN:VTODO", f"UID:{uid}",
             f"DTSTAMP:{_utc(now)}", f"CREATED:{_utc(now)}", f"LAST-MODIFIED:{_utc(now)}", f"SUMMARY:{nc._ics_escape(title)}",
             "STATUS:NEEDS-ACTION", "PERCENT-COMPLETE:0"]
    if due is not None:
        lines.append(f"DUE:{_utc(due)}")
    if description:
        lines.append(f"DESCRIPTION:{nc._ics_escape(description)}")
    lines += ["END:VTODO", "END:VCALENDAR"]
    return "\r\n".join(lines) + "\r\n"


def create_task(title: str, due=None, list_name=None, description: str = "") -> dict:
    title = (title or "").strip()
    if not title:
        raise TaskError("Dimmi cosa devo ricordarti.")
    cal = pick_list(list_name)
    uid = f"{uuid.uuid4()}@edith"
    try:
        cal.save_todo(build_ical(uid, title[:300], due, description))
    except Exception as e:
        nc.invalidate_cache()
        raise TaskError(f"Non sono riuscito a salvare il promemoria ({e.__class__.__name__}).") from e
    logger.info("[OK] Promemoria «%s» aggiunto alla lista «%s» (scadenza %s)", title, cal.name, due)
    return {"uid": uid, "title": title, "due": due, "list": cal.name}


def _put(v, name, value):
    v.contents.pop(name, None)
    v.add(name).value = value


def set_done(obj, done: bool) -> None:
    """Spunta (o riapre) un promemoria già trovato con open_tasks/find_by_uid."""
    try:
        v = obj.vobject_instance.vtodo
        now = datetime.now(dutz.tzutc())
        if done and hasattr(v, "rrule"):             # ricorrente: ci pensa la libreria (crea la prossima occorrenza)
            obj.complete(handle_rrule=True)
            return
        if done:
            _put(v, "status", "COMPLETED")
            _put(v, "percent-complete", "100")
            _put(v, "completed", now)
        else:
            _put(v, "status", "NEEDS-ACTION")
            _put(v, "percent-complete", "0")
            v.contents.pop("completed", None)
        _put(v, "last-modified", now)
        obj.save()
    except Exception as e:
        nc.invalidate_cache()
        raise TaskError(f"Non sono riuscito ad aggiornare il promemoria ({e.__class__.__name__}).") from e


def find_by_uid(uid: str, list_name=None) -> dict:
    """Il promemoria con quell'identificativo (cerca anche tra i completati, per poterlo riaprire)."""
    for t in open_tasks(list_name, include_completed=True):
        if t["uid"] == uid:
            return t
    raise TaskError("Non trovo più questo promemoria (forse è stato cancellato).")


def set_done_by_uid(uid: str, done: bool, list_name=None) -> dict:
    t = find_by_uid(uid, list_name)
    if t["done"] != done:
        set_done(t["_obj"], done)
    return {"uid": uid, "title": t["title"], "done": done, "list": t["list"]}
