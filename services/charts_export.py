"""
Grafici di /grafico e /budgetview salvati in una cartella dei tuoi file Nextcloud.

  - Ogni giorno (di notte) aggiorna i due file del mese in corso: stesso nome, quindi il vecchio viene sostituito.
  - L'ultimo giorno del mese tiene anche una copia che NON viene più toccata: «Archivio mensile/2026-10 grafico_spese.png».
  - Se un mese è finito senza che la copia sia stata fatta (server spento…), la rifà il giorno dopo con i dati completi.
  - Se nel mese non ci sono spese i file non si toccano (restano quelli di prima).

Prova a mano, dal contenitore:   python3 -m services.charts_export          (aggiorna)
                                 python3 -m services.charts_export --info   (mostra le funzioni dei grafici trovate)
"""
import logging
import sys
from datetime import date, datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

from config import CHARTS_FOLDER
from services import notes_service as ns
from services.notes_service import NotesError

logger = logging.getLogger(__name__)

TZ = ZoneInfo("Europe/Rome")
ARCHIVE_DIR = "Archivio mensile"
FILE_SPESE = "grafico_spese.png"          # quello di /grafico
FILE_BUDGET = "budget.png"                # quello di /budgetview
_MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"]
_PNG = b"\x89PNG\r\n\x1a\n"


class ChartsError(Exception):
    """Errore comprensibile: si può mostrare così com'è."""


# ---------------------------------------------------------------------------
# Nextcloud (WebDAV)
# ---------------------------------------------------------------------------

def _url(parts: list) -> str:
    return ns.get_webdav_base_url() + "/" + "/".join(quote(p, safe="") for p in parts)


def _explain(status: int) -> str:
    return {401: "Nextcloud rifiuta le credenziali", 403: "non ho il permesso di scrivere in quella cartella", 404: "Nextcloud non trova la cartella (controlla CHARTS_FOLDER nel .env)",
            507: "non c'è più spazio su Nextcloud",
            423: "il file è bloccato (aperto da qualche parte?)", 413: "il file è troppo grande per Nextcloud"}.get(status, f"risposta inattesa di Nextcloud (HTTP {status})")


def _ensure_folder(parts: list) -> None:
    for i in range(1, len(parts) + 1):
        res = ns._req("MKCOL", _url(parts[:i]))               # 201 creata, 405 esiste già
        if res.status_code not in (201, 405):
            raise ChartsError(f"Non riesco a creare la cartella «{'/'.join(parts[:i])}»: {_explain(res.status_code)}.")


def _put(parts: list, name: str, data: bytes) -> None:
    url = _url(parts + [name])
    res = ns._req("PUT", url, data=data, headers={"Content-Type": "image/png"})
    if res.status_code in (404, 409):                         # cartella mancante (a seconda della versione Nextcloud risponde 409 o 404): la creo e riprovo
        _ensure_folder(parts)
        res = ns._req("PUT", url, data=data, headers={"Content-Type": "image/png"})
    if res.status_code not in (200, 201, 204):
        raise ChartsError(f"Non riesco a salvare «{name}» in «{'/'.join(parts)}»: {_explain(res.status_code)}.")


def _exists(parts: list, name: str) -> bool:
    res = ns._req("PROPFIND", _url(parts + [name]), headers={"Depth": "0"})
    return res.status_code in (200, 207)


# ---------------------------------------------------------------------------
# Disegno: le stesse funzioni di /grafico e /budgetview
# ---------------------------------------------------------------------------

def _png(obj, what: str) -> bytes:
    """I grafici possono tornare come bytes o come BytesIO: li porto a bytes e controllo che sia davvero un PNG."""
    if obj is None:
        raise ChartsError(f"La funzione del grafico «{what}» non ha restituito nulla.")
    data = obj if isinstance(obj, (bytes, bytearray)) else (obj.getvalue() if hasattr(obj, "getvalue") else obj.read() if hasattr(obj, "read") else None)
    if not isinstance(data, (bytes, bytearray)) or not bytes(data).startswith(_PNG):
        raise ChartsError(f"Il grafico «{what}» non è un'immagine PNG (tipo ricevuto: {type(obj).__name__}).")
    return bytes(data)


def _currency(uid: int) -> str:
    try:
        from services import finance_nl
        return finance_nl._currency(uid)
    except Exception:
        return "€"


def render(uid: int, start: date, end: date, label: str) -> dict:
    """{nome file: PNG} per il periodo. Vuoto se non ci sono spese. Qui si adattano le chiamate se le funzioni dei grafici cambiano."""
    try:
        import charts
        import database as db
    except ImportError as e:
        raise ChartsError(f"Non trovo il modulo dei grafici del FinanceBot ({e}).") from e
    rows = db.get_category_totals(uid, str(start), str(end), "spesa")
    if not rows:
        return {}
    out = {FILE_SPESE: _png(charts.pie_chart_categorie(rows, title=f"Spese {label}"), "spese per categoria")}
    budgets = db.get_budgets(uid)
    if budgets:
        out[FILE_BUDGET] = _png(charts.bar_budget_comparison(budgets, rows, _currency(uid)), "budget")
    return out


# ---------------------------------------------------------------------------
# Il lavoro di ogni notte
# ---------------------------------------------------------------------------

def _month_name(d: date) -> str:
    return f"{_MESI[d.month - 1]} {d.year}"


def _archive_name(month_first: date, name: str) -> str:
    return f"{month_first:%Y-%m} {name}"


def run(uid: int, now: datetime = None) -> dict:
    """Aggiorna i grafici del mese in corso e, se serve, l'archivio. Ritorna un resoconto."""
    now = (now or datetime.now(TZ)).astimezone(TZ)
    today = now.date()
    first = today.replace(day=1)
    root = [p for p in CHARTS_FOLDER.split("/") if p.strip()] or ["Grafici EDITH"]      # «Documenti/Grafici» = una cartella dentro l'altra
    report = {"folder": "/".join(root), "updated": [], "archived": [], "notes": []}
    try:
        files = render(uid, first, today, f"{_month_name(today)} (aggiornato {now:%d/%m %H:%M})")
        if files:
            for name, data in files.items():
                _put(root, name, data)                         # stesso nome: il vecchio viene sostituito
                report["updated"].append(name)
        else:
            report["notes"].append("nessuna spesa nel mese: non ho toccato i file")

        archive = root + [ARCHIVE_DIR]
        if (today + timedelta(days=1)).day == 1 and files:     # ultimo giorno del mese: la copia di fine mese
            for name, data in files.items():
                _put(archive, _archive_name(first, name), data)
                report["archived"].append(_archive_name(first, name))
        prev_last = first - timedelta(days=1)                  # mese scorso: se manca la copia, la rifaccio con i dati completi
        prev_first = prev_last.replace(day=1)
        if not _exists(archive, _archive_name(prev_first, FILE_SPESE)):
            old = render(uid, prev_first, prev_last, f"{_month_name(prev_first)} (finale)")
            for name, data in old.items():
                _put(archive, _archive_name(prev_first, name), data)
                report["archived"].append(_archive_name(prev_first, name))
    except NotesError as e:
        raise ChartsError(str(e)) from e
    return report


def describe(report: dict) -> str:
    """Il resoconto in due righe, per Telegram."""
    parts = []
    if report["updated"]:
        parts.append(f"✅ Aggiornati in «{report['folder']}»: " + ", ".join(report["updated"]))
    if report["archived"]:
        parts.append("🗂️ Copia mensile salvata: " + ", ".join(report["archived"]))
    parts += [f"ℹ️ {n}" for n in report["notes"]]
    return "\n".join(parts) or "Niente da aggiornare."


# ---------------------------------------------------------------------------
# Prova a mano
# ---------------------------------------------------------------------------

def _info() -> str:
    import inspect
    lines = []
    for modname in ("charts", "database"):
        try:
            mod = __import__(modname)
        except ImportError as e:
            lines.append(f"{modname}: NON TROVATO ({e})")
            continue
        lines.append(f"== {modname}")
        for n, f in inspect.getmembers(mod, inspect.isfunction):
            if f.__module__ == modname and (modname == "charts" or any(k in n for k in ("total", "budget", "daily"))):
                lines.append(f"  {n}{inspect.signature(f)}")
    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    if "--info" in sys.argv:
        print(_info())
        sys.exit(0)
    import config
    uid = int(config.TELEGRAM_CHAT_ID) if str(config.TELEGRAM_CHAT_ID).lstrip("-").isdigit() else (sorted(config.FINANCE_USER_IDS) or [0])[0]
    try:
        print(describe(run(uid)))
    except ChartsError as e:
        print("❌", e)
        sys.exit(1)
