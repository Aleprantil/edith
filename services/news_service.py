"""
Legge le notizie dall'app Nextcloud News (API REST v1-3).

Non scarica né salva articoli o immagini: chiede a News solo titolo, link e data.
L'unica cosa che scrive è data/news_state.json, con gli ID già inviati (qualche KB),
così il riepilogo di mezzogiorno non ripete le notizie del mattino.

Uso a due tempi, per non perdere notizie se l'invio a Telegram fallisce:
    groups = collect_digest()   # cosa c'è di nuovo (non segna nulla)
    ... invio ...
    commit(groups)              # ora le segno come inviate
"""
import json
import logging
import os
import threading
import time

import requests

from config import (
    CALDAV_PASSWORD, CALDAV_URL, CALDAV_USER,
    NEWS_FOLDERS, NEWS_MAX_AGE_HOURS, NEWS_PER_FOLDER, NEWS_TRANSLATE,
)
from services.gemini_service import translate_headlines

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 20
FETCH_PER_FEED = 15          # solo i più recenti di ogni feed: News manda anche il testo completo, meno ne chiedo meglio è
SOURCE_MAX = 24              # lunghezza massima del nome della fonte mostrato accanto al titolo
SENT_TTL = 4 * 24 * 3600     # per quanto ricordo un ID già inviato
STATE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "news_state.json")

_session = requests.Session()
_session.auth = (CALDAV_USER, CALDAV_PASSWORD)
_lock = threading.Lock()


class NewsError(Exception):
    """News non raggiungibile o risposta inattesa."""


def _api_base() -> str:
    url = (CALDAV_URL or "").rstrip("/")
    root = url.split("/remote.php/dav")[0] if "/remote.php/dav" in url else url
    return f"{root}/index.php/apps/news/api/v1-3"


def _get(path: str, **params) -> dict:
    try:
        r = _session.get(
            f"{_api_base()}{path}", params=params, timeout=HTTP_TIMEOUT,
            headers={"Accept": "application/json"},
        )
    except requests.RequestException as e:
        raise NewsError(f"Nextcloud non raggiungibile ({e.__class__.__name__})") from e

    if r.status_code in (401, 403):
        raise NewsError("accesso a News negato: controlla utente e password")
    if r.status_code == 404:
        raise NewsError("app News non trovata (404): è installata e attiva?")
    if r.status_code != 200:
        raise NewsError(f"News ha risposto HTTP {r.status_code}")
    try:
        return r.json()
    except ValueError as e:
        raise NewsError("risposta di News non valida") from e


# ---------------------------------------------------------------------------
# Stato: ID già inviati
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) and isinstance(data.get("sent"), dict) else {"sent": {}}
    except (OSError, ValueError):
        return {"sent": {}}


def _save_state(state: dict):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_PATH)   # scrittura atomica


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def _interleave(lists: list, limit: int) -> list:
    """Prende a turno un titolo da ogni feed, così una fonte molto prolifica non copre le altre."""
    picked, i = [], 0
    while len(picked) < limit and any(i < len(lst) for lst in lists):
        for lst in lists:
            if i < len(lst) and len(picked) < limit:
                picked.append(lst[i])
        i += 1
    return picked


_DEFAULT = object()


def _structure():
    """Feed e cartelle di News: (lista feed, {id cartella: nome})."""
    feeds = _get("/feeds").get("feeds") or []
    folders = {f.get("id"): f.get("name", "") for f in (_get("/folders").get("folders") or [])}
    return feeds, folders


def collect_digest(now: float = None, *, only_folders=None, include_sent: bool = False,
                   per_folder: int = None, max_age_hours=_DEFAULT, structure=None) -> list:
    """
    Notizie (di default: nuove, non ancora inviate e non più vecchie di NEWS_MAX_AGE_HOURS), raggruppate per
    CARTELLA di News (i feed senza cartella restano da soli):
    [{"folder": str, "feed": str, "items": [{"id","title","url","pub","source","line"}], "all_ids": [...]}]
    "feed" è il nome del feed solo quando il gruppo ne contiene uno; "source" va mostrata se ce ne sono più di uno.

    Per le richieste a voce/testo («ultime news sul calcio») si usano le opzioni:
    only_folders=[nomi] limita alle cartelle indicate, include_sent=True ripropone anche le già inviate,
    per_folder=N cambia quanti titoli mostrare, max_age_hours=None toglie il limite di età.
    """
    now = time.time() if now is None else now
    limit = NEWS_PER_FOLDER if per_folder is None else per_folder

    feeds, folders = structure if structure else _structure()
    if only_folders is not None:
        wanted = {n.lower() for n in only_folders}
    else:
        wanted = {n.lower() for n in NEWS_FOLDERS}

    with _lock:
        sent = set() if include_sent else set(_load_state()["sent"])

    max_age = NEWS_MAX_AGE_HOURS if max_age_hours is _DEFAULT else max_age_hours
    min_pub = 0 if max_age is None else now - max_age * 3600
    buckets = {}   # chiave gruppo -> {"folder", "feeds": {titolo feed: [candidati]}}
    for feed in feeds:
        folder = folders.get(feed.get("folderId"), "")
        if wanted and folder.lower() not in wanted:
            continue

        # Una richiesta alla volta (poca CPU/RAM): ogni risposta contiene il testo degli articoli,
        # quindi tengo solo i quattro campi che servono e lascio liberare il resto.
        data = _get("/items", type=0, id=feed["id"], getRead="true",
                    batchSize=FETCH_PER_FEED, offset=0, oldestFirst="false")

        feed_title = (feed.get("title") or "Feed").strip()
        candidates = []
        for it in data.get("items") or []:
            iid = str(it.get("id"))
            pub = it.get("pubDate") or 0
            title = (it.get("title") or "").strip()
            if not title or iid in sent or (pub and pub < min_pub):
                continue
            candidates.append({"id": iid, "title": title, "url": it.get("url") or "", "pub": pub,
                               "source": feed_title[:SOURCE_MAX]})
        del data

        if candidates:
            candidates.sort(key=lambda x: x["pub"], reverse=True)
            key = folder.lower() if folder else f"feed:{feed['id']}"
            b = buckets.setdefault(key, {"folder": folder, "feeds": {}})
            b["feeds"][feed_title] = candidates

    groups = []
    for b in buckets.values():
        lists = list(b["feeds"].values())
        picked = _interleave(lists, limit)
        picked.sort(key=lambda x: x["pub"], reverse=True)
        groups.append({
            "folder": b["folder"],
            "feed": next(iter(b["feeds"])) if len(lists) == 1 else "",
            "items": picked,
            "all_ids": [c["id"] for lst in lists for c in lst],   # anche gli scartati: non li ripropongo dopo
        })

    # Titoli in italiano, con una sola chiamata a Gemini; se fallisce restano gli originali
    titles = {it["id"]: it["title"] for g in groups for it in g["items"]}
    translated = translate_headlines(titles) if (NEWS_TRANSLATE and titles) else {}
    for g in groups:
        for it in g["items"]:
            it["line"] = translated.get(it["id"], it["title"])

    logger.info("News: %d gruppi con novità, %d titoli", len(groups), len(titles))
    return groups


# Parole che l'utente può usare -> nome della cartella di News (se esiste)
TOPIC_ALIASES = {
    "calcio": "sport", "serie a": "sport", "champions": "sport", "tennis": "sport", "formula 1": "sport",
    "motori": "sport", "basket": "sport",
    "tecnologia": "tech", "informatica": "tech", "intelligenza artificiale": "tech", "ai": "tech",
    "politica": "italia", "cronaca": "italia", "italiane": "italia",
    "estero": "mondo", "esteri": "mondo", "internazionali": "mondo", "geopolitica": "mondo", "guerra": "mondo",
    "finanza": "economia", "borsa": "economia", "mercati": "economia",
    "sicurezza": "cyber", "cybersecurity": "cyber", "sicurezza informatica": "cyber", "hacker": "cyber",
    "wikipedia": "wiki",
}
ALL_TOPICS = {"", "tutte", "tutto", "tutti", "generali", "generale", "news", "notizie", "ultime", "novità"}


def resolve_topic(topic, folder_names: list):
    """
    Tema chiesto dall'utente -> lista di nomi di cartella. None = tutte le cartelle.
    Solleva NewsError se non corrisponde a nessuna cartella di News.
    """
    t = (str(topic).strip().lower() if topic else "")
    if t in ALL_TOPICS:
        return None
    by_lower = {n.lower(): n for n in folder_names if n}
    candidates = [t, TOPIC_ALIASES.get(t, "")]
    for c in candidates:
        if c in by_lower:
            return [by_lower[c]]
    for name_l, name in by_lower.items():          # «notizie di sport» -> sport
        if name_l in t or t in name_l:
            return [name]
    elenco = ", ".join(sorted(by_lower.values())) or "nessuna"
    raise NewsError(f"in News non c'è una cartella per «{topic}». Cartelle disponibili: {elenco}")


def collect_latest(topic=None, topic_items: int = 8) -> tuple:
    """
    «Dimmi le ultime news su X»: gli articoli più recenti del tema, anche se già inviati, senza limite di età
    e senza segnarli come inviati (i riepiloghi automatici restano com'erano).
    Ritorna (gruppi, nomi_cartelle_scelte o None).
    """
    structure = _structure()
    chosen = resolve_topic(topic, list(structure[1].values()))
    groups = collect_digest(only_folders=chosen, include_sent=True, max_age_hours=None,
                            per_folder=topic_items if chosen else None, structure=structure)
    return groups, chosen


def feed_status(now: float = None) -> list:
    """
    Fotografia dello stato dei feed (sola lettura, non cambia nulla): per ognuno l'articolo più recente,
    quanti sarebbero ancora da inviare e gli eventuali errori di aggiornamento segnalati da News.
    """
    now = time.time() if now is None else now
    feeds, folders = _structure()
    with _lock:
        sent = set(_load_state()["sent"])
    min_pub = now - NEWS_MAX_AGE_HOURS * 3600
    rows = []
    for feed in feeds:
        data = _get("/items", type=0, id=feed["id"], getRead="true",
                    batchSize=FETCH_PER_FEED, offset=0, oldestFirst="false")
        items = data.get("items") or []
        pubs = [it.get("pubDate") or 0 for it in items if it.get("pubDate")]
        new = sum(1 for it in items if str(it.get("id")) not in sent and (not it.get("pubDate") or it["pubDate"] >= min_pub))
        rows.append({
            "feed": (feed.get("title") or "Feed").strip(), "folder": folders.get(feed.get("folderId"), ""),
            "newest_pub": max(pubs) if pubs else None, "count": len(items), "new": new,
            "error": (feed.get("lastUpdateError") or "").strip(), "error_count": feed.get("updateErrorCount") or 0,
        })
        del data
    return rows


def commit(groups: list, now: float = None):
    """Segna come inviate le notizie di `groups` (da chiamare dopo l'invio riuscito)."""
    now = time.time() if now is None else now
    with _lock:
        state = _load_state()
        for g in groups:
            for iid in g.get("all_ids", []):
                state["sent"][iid] = now
        state["sent"] = {k: v for k, v in state["sent"].items() if now - v < SENT_TTL}
        _save_state(state)
