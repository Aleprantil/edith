"""
I moduli (plugin) di EDITH.

La BASE c'è sempre: il motore, Gemini, la pagina web, il bot Telegram, l'accesso sicuro. I moduli si scelgono nel .env:

    EDITH_PLUGINS=calendar,tasks,notes,memory,weather,news,finance,server

Senza la riga sono tutti attivi; EDITH_PLUGINS=none lascia solo la base. Ogni modulo dichiara qui:
- le richieste che Gemini può riconoscere (intents) e il suo pezzo di istruzioni (fields, guide): un modulo spento
  sparisce anche da quello che Gemini sa fare, quindi non può essere chiamato per sbaglio;
- le schede della pagina (cards);
- i moduli da cui dipende (requires) e se ha bisogno di Nextcloud.
Comandi e messaggi automatici del bot Telegram si accendono con on("nome").
"""
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Plugin:
    name: str
    title: str
    description: str
    intents: tuple = ()
    fields: str = ""                 # campi JSON in più che Gemini deve riempire
    guide: str = ""                  # come riconoscere le richieste del modulo
    cards: tuple = ()                # schede della pagina
    requires: tuple = ()             # altri moduli necessari
    needs_nextcloud: bool = False


CATALOG = {p.name: p for p in (
    Plugin(
        "calendar", "Calendario", "Agenda: impegni, crea, sposta, cancella, quando sei libero (Nextcloud).",
        intents=("create_event", "get_events", "update_event", "delete_event", "find_free_slots"),
        fields='''
    - "location": luogo esplicito se menzionato, altrimenti ""
    - "rrule": regola iCalendar valida se ricorrente, altrimenti null
    - "original_time": data/ora originale per update_event o delete_event. IMPORTANTE: se l'utente dice "cancella evento di mercoledì" o "elimina l'evento del 30", imposta 'original_time' (o 'start_time') alla data del giorno indicato (es. "2026-09-30T00:00:00").''',
        guide='''
    Regole per domande come "quando ho calcio?" o "quando vedo Marco questa settimana?":
    - Intent: "get_events"
    - Se chiede "questa settimana": imposta 'start_time' all'inizio della giornata corrente (00:00:00) e 'end_time' a domenica alle 23:59:59.
    - Se non specifica un periodo ma chiede genericamente "quando ho X" o "quando vedo X": imposta 'start_time' ad adesso e 'end_time' a 7 o 14 giorni da adesso.
    - 'title': la parola chiave cercata; 'category': il calendario relativo se è tra le categorie.
    - "create_event": Aggiungere o programmare un nuovo evento (es. "segna", "aggiungi", "ricordami di").
    - "get_events": Vedere impegni o cosa fare in un periodo (es. "cosa faccio?", "impegni di oggi").
    - "update_event": Spostare o rimandare un evento.
    - "delete_event": Cancellare o annullare un evento.
    - "find_free_slots": SCEGLI QUESTO se l'utente chiede quando è LIBERO, quando ha TEMPO, o cerca buchi/slot. (es. "quando sono libero domani?", "ho buchi oggi?"). Imposta in 'start_time' la data del giorno richiesto con orario T00:00:00.''',
        cards=("today",), needs_nextcloud=True),
    Plugin(
        "tasks", "Promemoria", "Cose da fare con scadenza, da spuntare (Nextcloud Tasks).",
        intents=("add_task", "complete_task", "get_tasks"),
        fields='''
    - "task_list": solo per add_task, complete_task e get_tasks: il nome della lista di promemoria SE l'utente la nomina (es. "todo", "to do", "spesa"), altrimenti null
    - "due_day": solo per add_task: la data "YYYY-MM-DD" SOLO se l'utente indica un giorno ("domani", "oggi", "giovedì", "il 15", "entro domani"), altrimenti null. Non inventare un giorno che non ha detto
    - "due_clock": solo per add_task: l'orario "HH:MM" SOLO se l'utente indica un orario ("entro le 12", "alle 18", "entro le 9 e mezza"), altrimenti null. Non inventare un orario che non ha detto''',
        guide='''
    - "add_task": L'utente vuole un PROMEMORIA o una cosa da fare in una lista (es. "aggiungimi un promemoria domani: da pagina 20 a 30", "promemoria: comprare il latte", "ricordami di chiamare Marco entro le 12", "aggiungi alla lista to do ..."). Scegli add_task quando dice "promemoria", "to do"/"todo", "da fare", "task" o indica una scadenza con "entro". NON è create_event: un evento è un impegno di calendario con un orario d'inizio (lezione, appuntamento, allenamento). In 'title' metti SOLO il testo da ricordare, senza parole come "promemoria", "aggiungimi", "domani", "entro" (es. "da pagina 20 a 30", "chiamare Marco"). Imposta 'due_day' e 'due_clock' come indicato sopra (se dice solo "entro le 12", 'due_day' è null e 'due_clock' è "12:00"; se dice solo "domani", 'due_day' è la data di domani e 'due_clock' è null).
    - "complete_task": L'utente vuole SEGNARE COME FATTO un promemoria (es. "spunta promemoria da pagina 20 a 30", "segna come fatto il promemoria della spesa", "ho finito da pagina 20 a 30", "completato chiamare Marco"). In 'title' metti le parole che identificano il promemoria, senza "promemoria" né "spunta" (es. "da pagina 20 a 30"). 'task_list' solo se nominata.
    - "get_tasks": L'utente vuole VEDERE i promemoria o le cose da fare (es. "che promemoria ho?", "cosa devo fare?", "mostrami la lista to do"). 'task_list' solo se nominata. NON è get_events: get_events riguarda gli impegni del calendario.''',
        cards=("tasks",), needs_nextcloud=True),
    Plugin(
        "notes", "Note", "Appunti e liste, da creare e rileggere (Nextcloud Notes).",
        intents=("add_note", "get_notes"),
        guide='''
    - "add_note": L'utente vuole salvare un appunto, un'idea, una lista o una nota (es. "prendi nota", "segna come nota", "ricordami per iscritto", "nota spesa: latte, uova"). Estrai un titolo sintetico in 'title', il corpo completo in 'description', e se deducibile una cartella in 'category' (es. università, spesa, idee, lavoro).
    - "get_notes": L'utente vuole leggere o consultare le sue note/appunti (es. "leggimi la nota della spesa", "quali note ho?", "mostrami gli appunti di università"). Estrai in 'title' il titolo specifico se richiesto (altrimenti stringa vuota o null) e in 'category' la cartella se specificata.''',
        cards=("notes",), needs_nextcloud=True),
    Plugin(
        "memory", "Memoria", "«Ricordati che…»: fatti su di te che EDITH usa per capirti meglio (una nota in Nextcloud).",
        needs_nextcloud=True),
    Plugin(
        "weather", "Meteo", "Meteo di oggi e dei prossimi giorni, alba e tramonto (Open-Meteo, gratis).",
        cards=()),
    Plugin(
        "news", "Notizie", "Titoli del giorno dalle cartelle dell'app News di Nextcloud, tradotti in italiano.",
        intents=("get_news",),
        fields='''
    - "news_topic": solo per get_news. Uno tra ["sport", "tech", "italia", "mondo", "economia", "cyber", "wiki"] oppure, se l'utente nomina un altro argomento, quella parola in minuscolo; null se vuole le notizie in generale. Negli altri casi null''',
        guide='''
    - "get_news": L'utente chiede le ULTIME NOTIZIE / news / novità su un argomento (es. "dimmi le ultime news sul calcio", "novità di tecnologia", "cosa succede nel mondo", "news generali"). Imposta 'news_topic' (calcio e tennis -> "sport", geopolitica ed estero -> "mondo", tecnologia -> "tech", sicurezza informatica -> "cyber", nessun argomento o "generali" -> null). NON è get_events: get_events riguarda gli impegni del calendario.''',
        cards=("news",), needs_nextcloud=True),
    Plugin(
        "finance", "Finanze", "Spese, entrate, budget e grafici (database locale; i grafici notturni vanno in Nextcloud, se c'è).",
        intents=("add_expense", "add_income", "get_finance_summary"),
        fields='''
    - "amount": importo in euro come numero (es. 12.5) per add_expense e add_income, altrimenti null
    - "finance_category": per add_expense uno tra [{spese}]; per add_income uno tra [{entrate}]; per get_finance_summary la categoria richiesta oppure null. Negli altri casi null
    - "period": solo per get_finance_summary, uno tra ["oggi", "ieri", "settimana", "settimana_scorsa", "mese", "mese_scorso", "anno", "anno_scorso", "personalizzato"] (se non specificato: "mese"). Usa "personalizzato" per tutto il resto (es. "l'altro ieri", "a settembre", "dal 1 al 15", "ultimi 7 giorni") e in quel caso imposta 'start_time' (primo giorno) e 'end_time' (ultimo giorno, incluso) come "YYYY-MM-DDT00:00:00". Negli altri casi null''',
        guide='''
    - "add_expense": L'utente registra una SPESA di denaro (es. "ho speso 12 euro di pizza", "pagato 50€ benzina", "ieri 30 euro al supermercato"). Estrai in 'amount' l'importo, in 'finance_category' la categoria più adatta, in 'title' una nota brevissima (es. "pizza", "benzina"). Se l'utente indica un giorno passato (es. "ieri", "sabato scorso") imposta 'start_time' a quel giorno alle 12:00:00, altrimenti null.
    - "add_income": L'utente registra un'ENTRATA di denaro (es. "ho ricevuto lo stipendio 1500", "incassato 200 euro di rimborso", "mi hanno regalato 50 euro"). Stessi campi di add_expense, con 'finance_category' scelta tra i tipi di entrata.
    - "get_finance_summary": L'utente chiede quanto ha speso o guadagnato, o il bilancio (es. "quanto ho speso questo mese?", "com'è andata la settimana?", "quanto ho speso in ristoranti quest'anno?", "saldo di oggi"). Imposta 'period' ("ieri" per «quanto ho speso ieri», "settimana_scorsa" per la settimana precedente, ecc.) e, solo se chiede una categoria precisa, 'finance_category'.
    - REGOLA SOLDI: se il messaggio parla di denaro (euro, €, speso, pagato, incassato, stipendio) NON è un evento di calendario né un promemoria: usa gli intent finanziari, anche se contiene "segna" o "aggiungi". "Segna 12 euro di pizza" è una spesa.''',
        cards=("finance",)),
    Plugin(
        "server", "Server", "Stato del server (CPU, memoria, dischi, Nextcloud) e avvisi automatici.",
        intents=("get_server_status",),
        guide='''
    - "get_server_status": L'utente chiede lo STATO DEL SERVER / del computer (es. "come sta il server?", "quanta RAM sta usando?", "che temperatura ha?", "il server è ok?", "quanto spazio ho sul disco?", "da quanto è acceso?"). Non servono altri campi.''',
        cards=("server",)),
)}

ALWAYS_CARDS = ("today",)          # la scheda «Oggi» c'è sempre (data e saluto); agenda e meteo dentro solo se i moduli sono attivi


def _parse(raw):
    if raw is None or not raw.strip() or raw.strip().lower() in ("all", "tutti", "*"):
        return list(CATALOG)
    if raw.strip().lower() in ("none", "nessuno", "base"):           # solo la base, nessun modulo
        return []
    names, unknown = [], []
    for x in raw.split(","):
        n = x.strip().lower()
        if not n:
            continue
        (names if n in CATALOG else unknown).append(n)
    if unknown:
        logger.warning("Moduli sconosciuti in EDITH_PLUGINS, ignorati: %s (disponibili: %s)", ", ".join(unknown), ", ".join(CATALOG))
    out = []
    for n in names:                                    # ordine del catalogo, senza doppioni
        if n not in out:
            out.append(n)
    missing = {n: [r for r in CATALOG[n].requires if r not in out] for n in out}
    for n, req in missing.items():
        if req:
            logger.warning("Il modulo «%s» richiede %s: lo spengo", n, ", ".join(req))
            out.remove(n)
    return [n for n in CATALOG if n in out]


_enabled = None


def enabled(refresh: bool = False) -> list:
    """I moduli attivi, dal .env (EDITH_PLUGINS). Senza la riga: tutti."""
    global _enabled
    if _enabled is None or refresh:
        _enabled = _parse(os.environ.get("EDITH_PLUGINS"))
    return list(_enabled)


def on(name: str) -> bool:
    return name in enabled()


def set_enabled(names) -> None:
    """Per i test e per la configurazione guidata. None = tutti; una lista vuota = solo la base, nessun modulo."""
    global _enabled
    if names is None:
        _enabled = _parse(None)
    else:
        _enabled = _parse(",".join(names)) if names else []


def plugin_for_intent(intent: str):
    return next((p for p in CATALOG.values() if intent in p.intents), None)


def intent_on(intent: str) -> bool:
    p = plugin_for_intent(intent)
    return p is None or p.name in enabled()            # intent senza modulo (es. «unknown») sono della base


def active_intents() -> list:
    return [i for n in enabled() for i in CATALOG[n].intents]


def card_on(card: str) -> bool:
    if card in ALWAYS_CARDS:
        return True
    p = next((p for p in CATALOG.values() if card in p.cards), None)
    return p is not None and p.name in enabled()


def prompt_parts() -> dict:
    """I pezzi di istruzioni per Gemini dei soli moduli attivi."""
    on_ = [CATALOG[n] for n in enabled()]
    return {"intents": active_intents() + ["unknown"],
            "fields": "".join(p.fields for p in on_),
            "guide": "".join(p.guide for p in on_)}


EXAMPLES = {"calendar": "«cosa faccio oggi?»", "tasks": "«promemoria domani: chiamare Marco»", "notes": "«prendi nota: comprare il latte»",
            "finance": "«ho speso 10 euro di pizza»", "news": "«ultime news sul calcio»", "server": "«come sta il server?»"}


def examples() -> list:
    """Qualche frase d'esempio, solo dei moduli attivi (per quando EDITH non capisce)."""
    ex = [EXAMPLES[n] for n in enabled() if n in EXAMPLES]
    return ex or ["«ciao»"]
