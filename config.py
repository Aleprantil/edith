import os
import sys
from datetime import time as dtime

from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
# Puoi cambiare modello dal .env senza toccare il codice (GEMINI_MODEL=...)
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
# Modello per ascoltare la voce (trascrizione). Se il tuo non accetta l'audio, metti qui uno che lo accetta, senza toccare il resto
GEMINI_AUDIO_MODEL = (os.getenv("GEMINI_AUDIO_MODEL") or GEMINI_MODEL).strip()
# Modello di riserva: se il principale è sovraccarico, finisce la quota o non risponde, la stessa richiesta passa a questo.
# «off» lo spegne. Se coincide con il principale non serve (nessuna riserva).
_fb = (os.getenv("GEMINI_FALLBACK_MODEL") or "gemini-flash-latest").strip()
GEMINI_FALLBACK_MODEL = "" if _fb.lower() in ("off", "none", "no", "0") else _fb
CALDAV_URL = os.getenv("CALDAV_URL")
CALDAV_USER = os.getenv("CALDAV_USER")
CALDAV_PASSWORD = os.getenv("CALDAV_PASSWORD")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY:
    raise ValueError("Mancano le variabili d'ambiente fondamentali nel file .env!")


# ---------------------------------------------------------------------------
# Accessi
#   TELEGRAM_CHAT_ID = tu: calendario, note, promemoria E finanze
#   ADMIN_IDS        = altri utenti (separati da virgola) ammessi SOLO alle finanze
#   Se sono entrambi vuoti il bot risponde a chiunque (main.py lo segnala nei log).
# ---------------------------------------------------------------------------

def _parse_ids(raw) -> set:
    return {int(i) for i in (raw or "").replace(";", ",").split(",") if i.strip().lstrip("-").isdigit()}


_owner_ids = _parse_ids(TELEGRAM_CHAT_ID)
_admin_ids = _parse_ids(os.getenv("ADMIN_IDS"))

CALENDAR_USER_IDS = _owner_ids or _admin_ids
FINANCE_USER_IDS = _admin_ids | _owner_ids

# Il decoratore @restricted del FinanceBot legge ADMIN_IDS dall'ambiente a ogni messaggio:
# lo allineo così i suoi controlli usano la stessa lista, senza modificare quel codice.
os.environ["ADMIN_IDS"] = ",".join(str(i) for i in sorted(FINANCE_USER_IDS))


# ---------------------------------------------------------------------------
# Moduli del FinanceBot (cartella finance/): restano invariati e si importano
# con i loro nomi originali (import database, from constants import ...).
# ---------------------------------------------------------------------------
FINANCE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "finance")
if os.path.isdir(FINANCE_DIR) and FINANCE_DIR not in sys.path:
    sys.path.insert(0, FINANCE_DIR)

# Database finanze: nel volume del progetto, così sopravvive alle ricreazioni del container
os.environ.setdefault("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "financebot.db"))


# ---------------------------------------------------------------------------
# Buongiorno e news (tutto opzionale, si cambia dal .env)
# ---------------------------------------------------------------------------

def _parse_time(raw, default: str) -> dtime:
    for candidate in (raw, default):
        try:
            return dtime.fromisoformat((candidate or "").strip())
        except ValueError:
            continue
    raise ValueError(f"Orario non valido nel .env: {raw!r} (usa il formato HH:MM, es. 07:30)")


def _parse_int(raw, default: int) -> int:
    try:
        return max(1, int(str(raw).strip()))
    except (TypeError, ValueError):
        return default


OWNER_NAME = (os.getenv("OWNER_NAME") or "").strip()                      # es. "Mario Rossi"
OWNER_NICKNAME = (os.getenv("OWNER_NICKNAME") or "").strip()              # come ti chiama la voce, es. "Mario" (se vuoto: OWNER_NAME)
MORNING_TIME = _parse_time(os.getenv("MORNING_TIME"), "07:30")            # buongiorno + agenda + news
NEWS_TIME = _parse_time(os.getenv("NEWS_TIME"), "12:00")                  # riepilogo news di metà giornata
NEWS_PER_FOLDER = _parse_int(os.getenv("NEWS_PER_FOLDER") or os.getenv("NEWS_PER_FEED"), 3)   # titoli per cartella/tema
NEWS_EVENING_TIME = _parse_time(os.getenv("NEWS_EVENING_TIME"), "19:00")  # news serali + domani in agenda
NEWS_MAX_AGE_HOURS = _parse_int(os.getenv("NEWS_MAX_AGE_HOURS"), 36)      # niente notizie più vecchie di così
NEWS_FOLDERS = [f.strip() for f in (os.getenv("NEWS_FOLDERS") or "").split(",") if f.strip()]  # vuoto = tutti i feed
NEWS_TRANSLATE = (os.getenv("NEWS_TRANSLATE", "1").strip().lower() not in ("0", "false", "no"))  # titoli in italiano


# ---------------------------------------------------------------------------
# Meteo (Open-Meteo: gratuito, senza chiave). Cambia le coordinate dal .env.
# ---------------------------------------------------------------------------

def _parse_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip().replace(",", "."))
    except ValueError:
        raise ValueError(f"{name} nel .env non è un numero: {raw!r} (es. 46.4983)")


WEATHER_LAT = _parse_float("WEATHER_LAT", 41.9028)    # predefinito: Roma (la configurazione guidata mette la tua città)
WEATHER_LON = _parse_float("WEATHER_LON", 12.4964)
WEATHER_PLACE = (os.getenv("WEATHER_PLACE") or "Roma").strip()
WEATHER_ENABLED = (os.getenv("WEATHER_ENABLED", "1").strip().lower() not in ("0", "false", "no"))


# ---------------------------------------------------------------------------
# Monitoraggio del server (tutto opzionale, si cambia dal .env)
# ---------------------------------------------------------------------------
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

MON_ENABLED = (os.getenv("MON_ENABLED", "1").strip().lower() not in ("0", "false", "no"))
MON_INTERVAL = _parse_int(os.getenv("MON_INTERVAL"), 60)                  # ogni quanti secondi controllare
MON_TEMP_CPU = _parse_float("MON_TEMP_CPU", 80.0)                         # °C: avviso temperatura CPU
MON_TEMP_DISK = _parse_float("MON_TEMP_DISK", 60.0)                       # °C: avviso temperatura dischi
MON_CPU_PCT = _parse_float("MON_CPU_PCT", 90.0)                           # % di CPU media per almeno MON_CPU_MINUTES
MON_CPU_MINUTES = _parse_int(os.getenv("MON_CPU_MINUTES"), 5)
MON_RAM_FREE_PCT = _parse_float("MON_RAM_FREE_PCT", 10.0)                 # avviso se la RAM disponibile scende sotto il %
MON_DISK_PCT = _parse_float("MON_DISK_PCT", 90.0)                         # avviso se un disco supera questo % di spazio usato
MON_NEWS_STALE_HOURS = _parse_float("MON_NEWS_STALE_HOURS", 12.0)       # avviso se il più recente articolo di tutti i feed è più vecchio di così (0 = spento)
MON_BATTERY_PCT = _parse_float("MON_BATTERY_PCT", 20.0)                   # avviso batteria bassa (portatile senza rete)
MON_BOOT_REPORT_DELAY = _parse_int(os.getenv("MON_BOOT_REPORT_DELAY"), 240)   # secondi di attesa dopo l'avvio prima del riepilogo
MON_PROBE_FILE = os.getenv("MON_PROBE_FILE") or os.path.join(_BASE_DIR, "data", "host_status.json")
MON_STATE_FILE = os.getenv("MON_STATE_FILE") or os.path.join(_BASE_DIR, "data", "monitor_state.json")
MON_ROOT_PATH = os.getenv("MON_ROOT_PATH") or _BASE_DIR                  # cartella sul disco di sistema (per misurarne lo spazio)


# ---------------------------------------------------------------------------
# Web app (interfaccia con la sfera e le schede). Gira dentro il bot, solo in locale:
# si espone con `tailscale serve`. Vedi web/README.md
# ---------------------------------------------------------------------------
WEB_ENABLED = (os.getenv("WEB_ENABLED", "1").strip().lower() not in ("0", "false", "no"))
WEB_HOST = (os.getenv("WEB_HOST") or "127.0.0.1").strip()         # NON cambiarlo: l'identità Tailscale è affidabile solo in locale
WEB_PORT = _parse_int(os.getenv("WEB_PORT"), 8787)
# Login Tailscale ammessi (separati da virgola), es. WEB_ALLOWED_LOGINS=tuo@email.com
WEB_ALLOWED_LOGINS = [x.strip().lower() for x in (os.getenv("WEB_ALLOWED_LOGINS") or "").split(",") if x.strip()]
WEB_PASSWORD = os.getenv("WEB_PASSWORD") or ""                    # alternativa/riserva: accesso con password

WEB_SECRET = os.getenv("WEB_SECRET") or ""                        # firma dei cookie; se vuoto la ricavo dal token del bot
ASSISTANT_NAME = (os.getenv("ASSISTANT_NAME") or "EDITH").strip()

# Promemoria (app Tasks di Nextcloud): la lista usata quando non ne nomini una
TASKS_DEFAULT_LIST = (os.getenv("TASKS_DEFAULT_LIST") or "TO DO").strip()
# Le sole liste di promemoria che EDITH legge e usa (separate da virgola). Di serie: solo quella predefinita.
# Altri calendari di Nextcloud che accettano i promemoria, e le altre liste, vengono ignorati.
TASKS_LISTS = [x.strip() for x in (os.getenv("TASKS_LISTS") or TASKS_DEFAULT_LIST).split(",") if x.strip()]

# Memoria di EDITH: una nota di Nextcloud (una cosa da ricordare per riga), che EDITH rilegge a ogni richiesta
MEMORY_NOTE_TITLE = (os.getenv("MEMORY_NOTE_TITLE") or "EDITH Memoria").strip()
MEMORY_MAX_FACTS = _parse_int(os.getenv("MEMORY_MAX_FACTS"), 40)

# Grafici di /grafico e /budgetview salvati ogni giorno in una cartella dei tuoi file Nextcloud
CHARTS_ENABLED = (os.getenv("CHARTS_ENABLED", "1").strip().lower() not in ("0", "false", "no"))
CHARTS_FOLDER = (os.getenv("CHARTS_FOLDER") or "Grafici EDITH").strip().strip("/")
CHARTS_TIME = _parse_time(os.getenv("CHARTS_TIME"), "23:55")

# Motore unico: il testo libero del bot Telegram passa dallo stesso motore della pagina di EDITH.
# ENGINE_UNIFIED=0 nel .env riporta il vecchio comportamento (interruttore di sicurezza).
ENGINE_UNIFIED = (os.getenv("ENGINE_UNIFIED", "1").strip().lower() not in ("0", "false", "no"))

# I calendari di Nextcloud tra cui Gemini sceglie quando crei o cerchi un impegno (i nomi dei TUOI calendari).
CALENDAR_CATEGORIES = [x.strip() for x in (os.getenv("CALENDAR_CATEGORIES") or "generale,lavoro,svago,università,sport").split(",") if x.strip()]
if "generale" not in [x.lower() for x in CALENDAR_CATEGORIES]:
    CALENDAR_CATEGORIES.append("generale")

# Le frasi di EDITH, per passione (pacchetti in web/static/frasi.json: marvel, linux, homelab, calcio, sci, studio, maker).
# Senza la riga ci sono tutti; le frasi legate al momento (giorni, meteo, promemoria, server) ci sono sempre.
QUOTE_PACKS = [x.strip().lower() for x in (os.getenv("EDITH_QUOTE_PACKS") or "").split(",") if x.strip()]
