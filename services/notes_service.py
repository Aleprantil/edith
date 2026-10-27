import logging
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import datetime
from urllib.parse import quote, unquote, urlparse
from zoneinfo import ZoneInfo

import requests

from config import CALDAV_URL, CALDAV_USER, CALDAV_PASSWORD

logger = logging.getLogger(__name__)

TZ_ROMA = ZoneInfo("Europe/Rome")
HTTP_TIMEOUT = 15

# Cartella dove si salvano le note. "Notes" era la vecchia cartella usata
# (per errore) per le note con categoria: la leggiamo ancora, ma non ci scriviamo più.
NOTES_ROOT = "Note"
LEGACY_ROOT = "Notes"

_session = requests.Session()
_session.auth = (CALDAV_USER, CALDAV_PASSWORD)


class NotesError(Exception):
    """Errore di comunicazione con Nextcloud (WebDAV)."""


def get_webdav_base_url() -> str:
    """Ricava l'endpoint WebDAV dei file dall'URL CalDAV di Nextcloud."""
    url = CALDAV_URL.rstrip("/")
    if "/remote.php/dav" in url:
        base = url.split("/remote.php/dav")[0]
        return f"{base}/remote.php/dav/files/{CALDAV_USER}"
    return url


def _origin() -> str:
    p = urlparse(CALDAV_URL)
    return f"{p.scheme}://{p.netloc}"


def _req(method: str, url: str, **kwargs):
    kwargs.setdefault("timeout", HTTP_TIMEOUT)
    try:
        return _session.request(method, url, **kwargs)
    except requests.RequestException as e:
        raise NotesError(f"Nextcloud non raggiungibile ({e.__class__.__name__})") from e


def _parse_http_date(value):
    """'Sat, 03 Oct 2026 14:05:00 GMT' -> epoch; None se manca o non è valida."""
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError):
        return None


def _clean_title(title: str) -> str:
    return "".join(c for c in (title or "") if c.isalnum() or c in (" ", "-", "_")).strip()


def _category_folder(category: str) -> str:
    cat = (category or "").strip()
    if not cat or cat.lower() == "generale":
        return ""
    return cat.capitalize()


def _folder_url(root: str, sub: str) -> str:
    parts = [root] + ([sub] if sub else [])
    return get_webdav_base_url() + "/" + "/".join(quote(p) for p in parts)


def _candidate_folders(category: str) -> list:
    """Cartelle in cui cercare, in ordine di priorità."""
    sub = _category_folder(category)
    folders = []
    if sub:
        folders += [(NOTES_ROOT, sub), (LEGACY_ROOT, sub)]
    folders.append((NOTES_ROOT, ""))   # se la categoria dedotta è sbagliata, prova la cartella principale
    return folders


def _list_folder(root: str, sub: str) -> list:
    res = _req("PROPFIND", _folder_url(root, sub) + "/", headers={"Depth": "1"})
    if res.status_code == 404:
        return []
    if res.status_code not in (200, 207):
        raise NotesError(f"Elenco note fallito (HTTP {res.status_code})")

    try:
        tree = ET.fromstring(res.content)
    except ET.ParseError as e:
        raise NotesError("Risposta WebDAV non valida") from e

    notes = []
    for response in tree.findall("{DAV:}response"):
        href_el = response.find("{DAV:}href")
        if href_el is None or not href_el.text:
            continue
        href = href_el.text
        filename = unquote(href.rstrip("/").split("/")[-1])
        if filename.endswith((".md", ".txt")):
            mod_el = response.find(".//{DAV:}getlastmodified")
            notes.append({"title": filename.rsplit(".", 1)[0], "filename": filename, "path": href,
                          "modified": _parse_http_date(mod_el.text if mod_el is not None else None)})
    return notes


def save_note_to_nextcloud(title: str, content: str, category: str = "") -> dict:
    """Salva una nota come file .md in Note/ (o Note/<Categoria>/) creando le cartelle se servono."""
    try:
        sub = _category_folder(category)
        folder_path = NOTES_ROOT + (f"/{sub}" if sub else "")

        clean_title = _clean_title(title)
        if not clean_title:
            clean_title = f"Nota_{datetime.now(TZ_ROMA).strftime('%Y%m%d_%H%M%S')}"

        created_at = datetime.now(TZ_ROMA).strftime("%d/%m/%Y %H:%M")
        body = f"# {title}\n*Creata il: {created_at}*\n\n{content.strip()}\n".encode("utf-8")
        file_url = f"{_folder_url(NOTES_ROOT, sub)}/{quote(clean_title)}.md"

        res = _req("PUT", file_url, data=body)

        if res.status_code == 409:  # cartella mancante: la creo e riprovo
            current = get_webdav_base_url()
            for part in folder_path.split("/"):
                current = f"{current}/{quote(part)}"
                _req("MKCOL", current)  # 201 creata, 405 esiste già
            res = _req("PUT", file_url, data=body)

        if res.status_code in (200, 201, 204):
            return {"success": True, "title": clean_title, "folder": folder_path}
        return {"success": False, "error": f"HTTP {res.status_code}: {res.text[:200]}"}

    except NotesError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.exception("Errore salvataggio nota")
        return {"success": False, "error": str(e)[:300]}


def read_note_exact(title: str, category: str = "") -> str | None:
    """Il testo della nota con QUEL titolo (niente ricerca approssimata); None se non esiste."""
    clean = _clean_title(title)
    if not clean:
        return None
    res = _req("GET", f"{_folder_url(NOTES_ROOT, _category_folder(category))}/{quote(clean)}.md")
    if res.status_code == 404:
        return None
    if res.status_code != 200:
        raise NotesError(f"Lettura della nota fallita (HTTP {res.status_code})")
    return res.content.decode("utf-8", errors="replace")


def write_note_raw(title: str, body: str, category: str = "") -> dict:
    """Scrive (o sostituisce) una nota con il testo ESATTO indicato, senza l'intestazione automatica di save_note_to_nextcloud."""
    try:
        sub = _category_folder(category)
        clean = _clean_title(title)
        if not clean:
            return {"success": False, "error": "titolo non valido"}
        file_url = f"{_folder_url(NOTES_ROOT, sub)}/{quote(clean)}.md"
        data = body.encode("utf-8")
        res = _req("PUT", file_url, data=data)
        if res.status_code == 409:                                 # cartella mancante: la creo e riprovo
            current = get_webdav_base_url()
            for part in [NOTES_ROOT] + ([sub] if sub else []):
                current = f"{current}/{quote(part)}"
                _req("MKCOL", current)
            res = _req("PUT", file_url, data=data)
        if res.status_code in (200, 201, 204):
            return {"success": True, "title": clean}
        return {"success": False, "error": f"HTTP {res.status_code}"}
    except NotesError as e:
        return {"success": False, "error": str(e)}


def list_notes(category: str = "") -> list:
    """Elenco delle note nella cartella principale o nella sottocartella della categoria."""
    sub = _category_folder(category)
    folders = [(NOTES_ROOT, sub)]
    if sub:
        folders.append((LEGACY_ROOT, sub))

    notes, seen = [], set()
    for root, s in folders:
        for n in _list_folder(root, s):
            if n["title"] not in seen:
                seen.add(n["title"])
                notes.append(n)
    return notes


def get_note_content(title: str, category: str = "") -> str | None:
    """Scarica il testo di una nota dal titolo (esatto, poi corrispondenza parziale)."""
    clean = _clean_title(title)
    if not clean:
        return None

    folders = _candidate_folders(category)

    for root, sub in folders:
        res = _req("GET", f"{_folder_url(root, sub)}/{quote(clean)}.md")
        if res.status_code == 200:
            return res.text

    for root, sub in folders:
        for n in _list_folder(root, sub):
            if clean.lower() in n["title"].lower():
                res = _req("GET", _origin() + n["path"])
                if res.status_code == 200:
                    return res.text

    return None
