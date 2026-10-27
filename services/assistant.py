"""
Motore dell'assistente, indipendente da Telegram: una frase entra, esce una risposta (HTML sicuro).

Lo usa la web app. Fa le stesse cose del bot (calendario, note, finanze, news, server) con tre differenze:
  - le azioni che modificano il calendario (spostare, cancellare) chiedono conferma con Sì/No;
  - ricorda gli ultimi scambi, così «spostalo alle 10» o «e domani?» si capiscono;
  - restituisce anche quali schede della pagina aggiornare.
"""
import asyncio
import html
import logging
import re
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, time as dtime
from html.parser import HTMLParser
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from services import digest, memory_service, monitor_service, news_service, nextcloud_service, notes_service, plugins, tasks_service
from services.gemini_service import AudioError, parse_user_input, speech_rewrite, transcribe_audio
from services.nextcloud_service import CalendarError
from services.news_service import NewsError
from services.notes_service import NotesError
from services.tasks_service import TaskError

logger = logging.getLogger(__name__)

TZ_ROMA = ZoneInfo("Europe/Rome")
NEUTRAL_CATEGORIES = ("generale", "lavoro", "svago")
MAX_TEXT = 500                 # caratteri massimi di una richiesta
HISTORY_TURNS = 4              # scambi ricordati
CONFIRM_TTL = 300              # secondi di validità di una conferma
MAX_SESSIONS = 20
SESSION_TTL = 6 * 3600
ESC = digest.esc

# Comandi della memoria: li riconosco io, senza chiedere a Gemini (più veloce e non si sbaglia con «ricordami di…»)
_REMEMBER = re.compile(r"^\s*(?:ricordati|ricorda|tieni\s+a\s+mente|memorizza)\s+che\s+(.+?)\s*[.!]*\s*$", re.I | re.S)
_SHOW_MEMORY = re.compile(r"^\s*(?:(?:cosa|che\s+cosa|che)\s+(?:ricordi|sai|ti\s+ricordi|hai\s+in\s+memoria|hai\s+memorizzato)(?:\s+di\s+me)?"
                          r"|(?:mostrami|dimmi|leggi|elenca)\s+(?:la\s+)?memoria)\s*\??\s*$", re.I)
_FORGET = re.compile(r"^\s*(?:dimentica|scordati|togli\s+dalla\s+memoria|cancella\s+dalla\s+memoria|elimina\s+dalla\s+memoria)\s+(?:che\s+)?(.+?)\s*[.!]*\s*$", re.I | re.S)


# ---------------------------------------------------------------------------
# HTML sicuro
# ---------------------------------------------------------------------------

class _Sanitizer(HTMLParser):
    """Lascia passare solo grassetto, corsivo, codice, a capo e link http(s). Tutto il resto diventa testo innocuo."""
    ALLOWED = {"b", "strong", "i", "em", "code", "a", "br"}
    DROP_CONTENT = {"script", "style", "iframe", "object", "embed", "svg", "math", "template", "noscript", "head", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.stack, self.skip = [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.DROP_CONTENT:
            self.skip += 1
            return
        if self.skip or tag not in self.ALLOWED:
            return
        if tag == "br":
            self.out.append("<br>")
            return
        if tag == "a":
            href = (dict(attrs).get("href") or "").strip()
            if re.match(r"^https?://", href, re.I):
                self.out.append(f'<a href="{html.escape(href, quote=True)}" target="_blank" rel="noopener noreferrer">')
                self.stack.append(("a", True))
            else:
                self.stack.append(("a", False))
            return
        self.out.append(f"<{tag}>")
        self.stack.append((tag, True))

    def handle_startendtag(self, tag, attrs):
        if tag == "br" and not self.skip:
            self.out.append("<br>")

    def handle_endtag(self, tag):
        if tag in self.DROP_CONTENT:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip or tag not in self.ALLOWED or tag == "br":
            return
        idx = next((i for i in range(len(self.stack) - 1, -1, -1) if self.stack[i][0] == tag), None)
        if idx is None:
            return
        while len(self.stack) > idx:
            name, emitted = self.stack.pop()
            if emitted:
                self.out.append(f"</{name}>")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(html.escape(data, quote=False))

    def result(self):
        while self.stack:
            name, emitted = self.stack.pop()
            if emitted:
                self.out.append(f"</{name}>")
        return "".join(self.out)


def sanitize_html(text: str) -> str:
    p = _Sanitizer()
    try:
        p.feed(text or "")
        p.close()
    except Exception:           # HTML irrecuperabile: lo trasformo tutto in testo
        return html.escape(text or "", quote=False)
    return p.result()


def md_to_html(text: str) -> str:
    """Markdown 'alla Telegram' (*grassetto*, _corsivo_, `codice`) -> HTML sicuro."""
    t = html.escape(text or "", quote=False)
    t = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\*([^*\n]+)\*", r"<b>\1</b>", t)
    t = re.sub(r"(?<![\w])_([^_\n]+)_(?![\w])", r"<i>\1</i>", t)
    return t


_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200d\u20e3]")


def to_plain(text: str, limit: int = 450) -> str:
    """Testo semplice da leggere a voce: senza tag, link ed emoji."""
    t = re.sub(r"<br\s*/?>", ". ", text or "")
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    t = re.sub(r"https?://\S+", "", t)
    t = _EMOJI.sub("", t)
    t = re.sub(r"[•·▪]", ",", t)
    t = re.sub(r"\s*\n+\s*", ". ", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" .,")
    return (t[: limit - 1].rsplit(" ", 1)[0] + "…") if len(t) > limit else t


# ---------------------------------------------------------------------------
# Risposta
# ---------------------------------------------------------------------------

@dataclass
class Reply:
    html: str
    ok: bool = True
    refresh: list = field(default_factory=list)       # schede da ricaricare: today, notes, finance, news, server
    confirm: dict = None                               # {"token","question","yes","no"}
    speak: str = ""
    heard: str = ""                                    # cosa ha capito dalla voce (solo per gli ordini parlati)
    say: str = field(default="", repr=False)           # frase parlata già pronta (senza chiamare Gemini)
    intent: str = field(default="", repr=False)

    def to_dict(self) -> dict:
        return {"html": self.html, "ok": self.ok, "refresh": self.refresh, "confirm": self.confirm, "speak": self.speak, "heard": self.heard}


def _ok(text_html: str, refresh=None, say: str = "") -> Reply:
    clean = sanitize_html(text_html)
    return Reply(html=clean, refresh=list(refresh or []), speak=to_plain(clean), say=say)


# --- come si dicono a voce orari, giorni e importi ---
_GIORNI = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
_MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"]
SPEECH_REWRITE = ("get_news", "get_server_status", "get_finance_summary", "get_notes")     # risposte lunghe: le riassume Gemini


def say_time(dt) -> str:
    h, m = dt.hour, dt.minute
    if (h, m) == (0, 0):
        return "a mezzanotte"
    if (h, m) == (12, 0):
        return "a mezzogiorno"
    base = "all'una" if h == 1 else f"alle {h}"
    return base if m == 0 else base + (" e mezza" if m == 30 else " e un quarto" if m == 15 else f" e {m}")


def say_day(d, now) -> str:
    delta = (d - now.date()).days
    if delta == 0:
        return "oggi"
    if delta == 1:
        return "domani"
    if delta == 2:
        return "dopodomani"
    if delta == -1:
        return "ieri"
    if 2 < delta < 7:
        return _GIORNI[d.weekday()]
    return f"il {d.day} {_MESI[d.month - 1]}"


def say_amount(x) -> str:
    try:
        v = round(abs(float(x)), 2)
    except (TypeError, ValueError):
        return "l'importo"
    euro, cent = int(v), int(round((v - int(v)) * 100))
    return f"{euro} euro" + (f" e {cent}" if cent else "")


def _speakable(text: str) -> str:
    return to_plain(text or "", 90)


def _err(text: str) -> Reply:
    clean = sanitize_html(f"❌ {ESC(text)}")
    return Reply(html=clean, ok=False, speak=to_plain(clean))


def parse_dt(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=TZ_ROMA)


def _fmt(dt) -> str:
    return dt.strftime("%d/%m/%Y %H:%M") if isinstance(dt, datetime) else (dt.strftime("%d/%m/%Y") if dt else "?")


def _run(coro):
    """Esegue una coroutine dal thread di lavoro (qui non c'è un ciclo asyncio attivo)."""
    return asyncio.run(coro)


def _finance():
    from services import finance_nl           # import tardivo: serve la cartella finance/ del FinanceBot
    return finance_nl


# ---------------------------------------------------------------------------
# Motore
# ---------------------------------------------------------------------------

class _Session:
    def __init__(self):
        self.history = deque(maxlen=HISTORY_TURNS)
        self.pending = {}                          # token -> (azione, scadenza, testo del «no»)
        self.last_seen = time.time()


FINANCE_INTENTS = {"add_expense", "add_income", "get_finance_summary"}


class Assistant:
    """Il motore: capisce cosa chiedi e lo fa. Lo usano sia la pagina di EDITH sia il bot Telegram.

    `features` dice cosa può fare chi parla: «calendar» = tutto tranne le finanze (agenda, note, promemoria, notizie, server, memoria),
    «finance» = le sue spese. None = tutto (la pagina web, che è solo tua)."""

    def __init__(self, owner_id: int = 0, clock=None, features=None):
        self.owner_id = owner_id
        self.clock = clock or (lambda: datetime.now(TZ_ROMA))
        self.features = None if features is None else frozenset(features)
        self.sessions = {}
        self.lock = threading.Lock()

    def _can(self, feature: str) -> bool:
        return self.features is None or feature in self.features

    # ----- sessioni --------------------------------------------------------
    def _session(self, sid: str) -> _Session:
        with self.lock:
            now = time.time()
            for k in [k for k, s in self.sessions.items() if now - s.last_seen > SESSION_TTL]:
                del self.sessions[k]
            if sid not in self.sessions and len(self.sessions) >= MAX_SESSIONS:
                oldest = min(self.sessions, key=lambda k: self.sessions[k].last_seen)
                del self.sessions[oldest]
            s = self.sessions.setdefault(sid, _Session())
            s.last_seen = now
            return s

    def _context(self, s: _Session) -> str:
        return "\n".join(f"Utente: {u}\nAssistente: {a}" for u, a in s.history)

    # ----- ingresso --------------------------------------------------------
    def _spoken(self, reply: Reply) -> str:
        """La frase da dire a voce: breve e discorsiva, non il testo a schermo letto tutto."""
        if reply.say:
            return reply.say
        if reply.confirm or not reply.ok:
            return to_plain(reply.html, 220)
        if reply.intent in SPEECH_REWRITE:
            out = speech_rewrite(to_plain(reply.html, 1500), reply.intent)
            if out:
                return out
        return to_plain(reply.html, 220)

    def transcribe(self, wav: bytes) -> str:
        return transcribe_audio(wav)

    def handle_voice(self, sid: str, wav: bytes) -> Reply:
        """Ordine parlato: trascrive con Gemini, poi segue la stessa strada del testo. La risposta è sempre pensata per essere detta."""
        try:
            text = self.transcribe(wav)
        except AudioError as e:
            r = _err(str(e))
            r.speak = to_plain(r.html, 160)
            return r
        if not text:
            r = _err("Non ho sentito bene, puoi ripetere?")
            r.speak = "Non ho sentito bene, puoi ripetere?"
            return r
        reply = self.handle(sid, text, spoken=True)
        reply.heard = text
        return reply

    def handle(self, sid: str, text: str, spoken: bool = False) -> Reply:
        text = (text or "").strip()[:MAX_TEXT]
        if not text:
            return _err("Scrivimi cosa vuoi fare.")
        s = self._session(sid)
        try:
            reply = self._dispatch(s, text)
            if spoken:
                reply.speak = self._spoken(reply)
        except (CalendarError, NotesError, NewsError, TaskError) as e:
            reply = _err(str(e))
        except Exception:
            logger.exception("Assistente: errore non gestito")
            reply = _err("Qualcosa è andato storto. Riprova tra poco (i dettagli sono nei log).")
        summary = to_plain(reply.html, 160) or "ok"
        s.history.append((text, summary))
        return reply

    def confirm(self, sid: str, token: str, yes: bool, spoken: bool = False) -> Reply:
        s = self._session(sid)
        entry = s.pending.pop(token, None)
        if not entry or entry[1] < time.time():
            return _err("La richiesta di conferma è scaduta. Ripetimi cosa vuoi fare.")
        if not yes:
            no_text = entry[2] if len(entry) > 2 and entry[2] else "Va bene, non faccio nulla."
            r = _ok(f"👍 {no_text}", say=no_text)
            if spoken:
                r.speak = r.say
            return r
        try:
            reply = entry[0]()
            if spoken:
                reply.speak = self._spoken(reply)
        except (CalendarError, NotesError, TaskError) as e:
            return _err(str(e))
        except Exception:
            logger.exception("Assistente: conferma fallita")
            return _err("Non sono riuscito a completare l'operazione.")
        s.history.append(("(conferma)", to_plain(reply.html, 160) or "ok"))
        return reply

    def _new_token(self, s: _Session, action, no_text: str = "") -> str:
        now = time.time()
        for k in [k for k, e in s.pending.items() if e[1] < now]:
            del s.pending[k]
        while len(s.pending) >= 9:
            s.pending.pop(next(iter(s.pending)))
        token = secrets.token_urlsafe(9)
        s.pending[token] = (action, now + CONFIRM_TTL, no_text)
        return token

    def _ask(self, s: _Session, question_html: str, action, yes: str = "Sì", no: str = "Annulla", say: str = "", no_text: str = "",
             listen: bool = True) -> Reply:
        token = self._new_token(s, action, no_text)
        q = sanitize_html(question_html)
        conf = {"token": token, "yes": yes, "no": no}
        if not listen:
            conf["listen"] = False                  # i pulsanti ci sono, ma la voce non si rimette in ascolto da sola
        return Reply(html=q, confirm=conf, speak=to_plain(q), say=say or to_plain(q) + " Dimmi sì o no.")

    def _ask_choice(self, s: _Session, question_html: str, options: list, say: str) -> Reply:
        """Più possibilità (al massimo 4): un pulsante per ciascuna, più «Annulla». options = [(etichetta, azione)]."""
        choices = [{"token": self._new_token(s, action), "label": label[:80]} for label, action in options[:4]]
        cancel = self._new_token(s, lambda: None)
        q = sanitize_html(question_html)
        return Reply(html=q, confirm={"token": cancel, "yes": "", "no": "Annulla", "choices": choices}, speak=to_plain(q), say=say)

    # ----- smistamento -----------------------------------------------------
    def _dispatch(self, s: _Session, text: str) -> Reply:
        now = self.clock()
        memory = ""
        if self._can("calendar") and plugins.on("memory"):   # la memoria è tua: chi ha accesso solo alle finanze non la vede né la usa
            mem_reply = self._memory_command(s, text)
            if mem_reply is not None:
                mem_reply.intent = "memory"
                return mem_reply
            memory = memory_service.prompt_block()       # le cose che mi hai chiesto di ricordare (mai un errore: al peggio è vuota)
        extra = {"memory": memory} if memory else {}
        parsed = parse_user_input(text, now.strftime("%Y-%m-%d %H:%M:%S"), self._context(s), **extra)
        if parsed.get("error"):
            return _err("Non riesco a interpretare il messaggio (Gemini non risponde). Riprova tra poco.")

        f = SimpleNamespace(
            intent=parsed.get("intent"),
            title=str(parsed.get("title") or "").strip(),
            start_str=parsed.get("start_time"), end_str=parsed.get("end_time"),
            category=parsed.get("category") or "svago", raw_category=parsed.get("category") or "",
            description=parsed.get("description") or "", location=parsed.get("location") or "",
            rrule=parsed.get("rrule"), amount=parsed.get("amount"),
            finance_category=parsed.get("finance_category"), period=parsed.get("period"),
            news_topic=parsed.get("news_topic"),
            task_list=(parsed.get("task_list") or None), due_day=parsed.get("due_day"), due_clock=parsed.get("due_clock"),
            explicit_original=bool(parsed.get("original_time")),
            original_str=parsed.get("original_time") or parsed.get("start_time"),
        )
        handler = {
            "create_event": self._create, "get_events": self._get_events, "update_event": self._update,
            "delete_event": self._delete, "find_free_slots": self._free_slots, "add_note": self._add_note,
            "get_notes": self._get_notes, "add_expense": self._expense, "add_income": self._income,
            "get_finance_summary": self._finance_summary, "get_news": self._news, "get_server_status": self._server,
            "add_task": self._add_task, "complete_task": self._complete_task, "get_tasks": self._get_tasks,
        }.get(f.intent)
        if handler is not None and not plugins.intent_on(f.intent):     # un modulo spento (di solito Gemini non lo propone nemmeno)
            p = plugins.plugin_for_intent(f.intent)
            return _ok(f"🧩 Il modulo «{p.title if p else f.intent}» non è attivo su questo EDITH.")
        if handler is None and not self._can("calendar"):
            return _ok("🤖 Non ho capito. Posso aiutarti con le finanze, ad esempio: «ho speso 10 euro di pizza».")
        if handler is not None:
            need = "finance" if f.intent in FINANCE_INTENTS else "calendar"
            if not self._can(need):
                return _err("⛔ Non sei autorizzato " + ("alle finanze." if need == "finance" else "a usare il calendario."))
        if handler is None:
            return _ok("🤖 Non ho capito cosa fare. Prova con: " + ", ".join(plugins.examples()) + ".")
        reply = handler(s, f, text, now)
        reply.intent = f.intent
        return reply

    # ----- calendario ------------------------------------------------------
    def _create(self, s, f, text, now):
        start_dt = parse_dt(f.start_str)
        if not start_dt:
            return _err("Non ho capito quando fissare l'evento. Indica giorno e ora.")
        title = f.title or "Senza titolo"
        r = nextcloud_service.create_calendar_event(
            title=title, start_time_str=f.start_str, description=f.description, calendar_name=f.category,
            rrule=f.rrule, end_time_str=f.end_str, location=f.location)
        if r.get("success") is not True:
            return _err(f"Errore nel salvataggio su Nextcloud: {r.get('message')}")
        lines = [f"✅ Evento salvato nel calendario <b>{ESC(r.get('calendar_used', f.category))}</b>"
                 + (f" (ricorrente: {ESC(f.rrule)})" if f.rrule else "") + ":",
                 f"<b>{ESC(title)}</b>" + (f"\n📍 {ESC(f.location)}" if f.location else ""),
                 f"📅 {_fmt(start_dt)}"]
        if r.get("conflict"):
            lines.append("\n⚠️ <b>Attenzione</b>: hai già un altro impegno a quell'ora.")
        say = f"Fatto. Ho messo {_speakable(title)} {say_day(start_dt.date(), now)} {say_time(start_dt)}." + (" Attenzione, hai già un altro impegno a quell'ora." if r.get("conflict") else "")
        return _ok("\n".join(lines), ["today"], say=say)

    def _get_events(self, s, f, text, now):
        start_dt = parse_dt(f.start_str) or datetime.combine(now.date(), dtime.min, tzinfo=TZ_ROMA)
        end_dt = parse_dt(f.end_str)
        if end_dt is None:
            topic = bool(f.title or (f.category and f.category.lower() not in NEUTRAL_CATEGORIES))
            end_dt = (start_dt + timedelta(days=7)) if (topic and not f.start_str) \
                else datetime.combine(start_dt.date(), dtime.max, tzinfo=TZ_ROMA)
        events = nextcloud_service.get_calendar_events(start_dt, end_dt)

        term = f.title.lower()
        cat = (f.category or "").lower().strip()
        has_cat = bool(cat) and cat not in NEUTRAL_CATEGORIES
        shown = events
        if term or has_cat:
            shown = [e for e in events
                     if (not term or term in str(e.get("title", "")).lower() or term in str(e.get("calendar", "")).lower())
                     and (not has_cat or cat in str(e.get("calendar", "")).lower() or cat in str(e.get("title", "")).lower())]
        # se chiedi della giornata di oggi, aggiungo anche i promemoria (se non si raggiungono, pazienza: gli impegni vengono prima)
        todos, n_todos = self._agenda_tasks(now) if (not term and not has_cat and start_dt.date() == now.date() and end_dt.date() == now.date()) else ([], 0)
        if not shown and not todos:
            return _ok(f"📅 Non ho trovato impegni per «{ESC(text)}».", say="Non ho trovato impegni.")
        if not shown:
            return _ok("📅 Oggi non hai impegni in calendario.\n\n☑️ <b>Promemoria</b>\n" + self._task_lines(todos, n_todos),
                       say="Oggi non hai impegni in calendario. " + self._say_tasks(todos, n_todos))
        lines = ["📅 <b>Ecco cosa ho trovato</b>"]
        for e in shown[:30]:
            st = e.get("start")
            when = st.strftime("%d/%m %H:%M") if isinstance(st, datetime) else (st.strftime("%d/%m") + " tutto il giorno" if st else "?")
            loc = f" 📍 {ESC(e['location'])}" if e.get("location") else ""
            lines.append(f"• <b>{ESC(when)}</b> {ESC(e.get('title', 'Senza titolo'))}{loc} <i>({ESC(e.get('calendar', ''))})</i>")
        say = self._say_events(shown, now)
        if todos:
            lines += ["", "☑️ <b>Promemoria</b>", self._task_lines(todos, n_todos)]
            say += " " + self._say_tasks(todos, n_todos)
        return _ok("\n".join(lines), say=say)

    def _say_events(self, shown, now) -> str:
        """«Oggi hai 3 impegni. Alle 9, Lezione; poi alle 11 e mezza, Call…» (al massimo tre, poi «e altri N»)."""
        def day_of(e):
            st = e.get("start")
            return st.date() if isinstance(st, datetime) else st

        def when(e, with_day):
            st, d = e.get("start"), day_of(e)
            clock = say_time(st) if isinstance(st, datetime) else "tutto il giorno"
            return f"{say_day(d, now)} {clock}" if with_day else clock
        n = len(shown)
        same_day = len({day_of(e) for e in shown}) == 1 and day_of(shown[0])
        items = [f"{when(e, not same_day)}, {_speakable(e.get('title', 'senza titolo'))}" for e in shown[:3]]
        head = (f"{say_day(same_day, now).capitalize()} hai " if same_day else "Hai ") + ("un impegno" if n == 1 else f"{n} impegni")
        items[0] = items[0][:1].upper() + items[0][1:]                       # «Alle 9, …» dopo il punto
        out = head + ". " + "; poi ".join(items) + "."
        if n > 3:
            out += f" E altri {n - 3}." if n - 3 > 1 else " E un altro."
        return out

    def _update(self, s, f, text, now):
        orig, new_start = parse_dt(f.original_str), parse_dt(f.start_str)
        if not f.title:
            return _err("Dimmi quale evento vuoi spostare.")
        if not orig or not new_start:
            return _err("Non ho capito quale evento spostare e a quando. Indica il giorno e il nuovo orario.")
        new_end = parse_dt(f.end_str)
        found = nextcloud_service.find_event(f.category, orig, f.title, 1, "spostare")
        if not found:
            return _err(f"Non trovo nessun evento «{f.title}» da spostare in quel giorno.")
        d, ev = found

        def action():
            nextcloud_service.update_found(ev, d, new_start, new_end)
            return _ok(f"🔄 <b>{ESC(d['title'])}</b> spostato a {_fmt(new_start)}.", ["today"],
                       say=f"Fatto, ho spostato {_speakable(d['title'])} {say_day(new_start.date(), now)} {say_time(new_start)}.")

        return self._ask(s, f"Sposto <b>{ESC(d['title'])}</b> ({_fmt(d.get('start'))}, {ESC(d.get('calendar', ''))}) "
                            f"a <b>{_fmt(new_start)}</b>?", action, yes="Sposta",
                         say=f"Vuoi che sposti {_speakable(d['title'])} {say_day(new_start.date(), now)} {say_time(new_start)}? Dimmi sì o no.")

    def _delete(self, s, f, text, now):
        if not f.title:
            return _err("Dimmi quale evento vuoi cancellare.")
        target = parse_dt(f.original_str) or datetime.combine(now.date(), dtime.min, tzinfo=TZ_ROMA)
        found = nextcloud_service.find_event(f.category, target, f.title, 1 if f.explicit_original else 14, "cancellare")
        if not found:
            return _err(f"Non ho trovato nessun evento «{f.title}» da cancellare.")
        d, ev = found

        def action():
            nextcloud_service.delete_found(ev, d)
            return _ok(f"🗑️ <b>{ESC(d['title'])}</b> cancellato.", ["today"], say=f"Fatto, ho cancellato {_speakable(d['title'])}.")

        st = d.get("start")
        quando = f" di {say_day(st.date(), now)} {say_time(st)}" if isinstance(st, datetime) else ""
        return self._ask(s, f"Cancello <b>{ESC(d['title'])}</b> del {_fmt(d.get('start'))} "
                            f"(<i>{ESC(d.get('calendar', ''))}</i>)?", action, yes="Cancella",
                         say=f"Vuoi che cancelli {_speakable(d['title'])}{quando}? Dimmi sì o no.")

    def _free_slots(self, s, f, text, now):
        a, b = parse_dt(f.start_str), parse_dt(f.end_str)
        if a and b:
            conflicts = nextcloud_service.get_calendar_events(a, b)
            overlap = []
            for e in conflicts:
                es = e.get("start")
                ee = e.get("end") or (es + timedelta(hours=1) if isinstance(es, datetime) else None)
                if isinstance(es, datetime) and isinstance(ee, datetime) and max(a, es) < min(b, ee):
                    overlap.append(e)
            if not overlap:
                return _ok(f"✅ Sì, sei libero dalle {a.strftime('%H:%M')} alle {b.strftime('%H:%M')}.", say="Sì, in quella fascia sei libero.")
            lines = ["❌ No, in quella fascia hai già:"]
            lines += [f"• <b>{ESC(e['title'])}</b> ({ESC(e['calendar'])}) alle {e['start'].strftime('%H:%M')}" for e in overlap]
            return _ok("\n".join(lines), say=f"No, in quella fascia hai già {_speakable(overlap[0]['title'])} {say_time(overlap[0]['start'])}.")
        slots = nextcloud_service.get_free_slots(a or now, f.category)
        if not slots:
            return _ok("Non hai tempo libero in quella data. 🥵", say="In quella data non hai tempo libero.")
        lines = ["✅ <b>I tuoi momenti liberi</b>"]
        lines += [f"• dalle {x.strftime('%H:%M')} alle {y.strftime('%H:%M')}" for x, y in slots]
        giorno = say_day((a or now).date(), now)
        parts = [f"dalle {x.hour}{'' if x.minute == 0 else ' e ' + str(x.minute)} alle {y.hour}{'' if y.minute == 0 else ' e ' + str(y.minute)}" for x, y in slots[:3]]
        return _ok("\n".join(lines), say=f"{giorno.capitalize()} sei libero " + ", poi ".join(parts) + ("." if len(slots) <= 3 else f", e altri {len(slots) - 3} momenti."))

    # ----- memoria ---------------------------------------------------------
    def _memory_command(self, s, text):
        m = _REMEMBER.match(text)
        if m:
            status, fact = memory_service.add(m.group(1))
            if status == "added":
                return _ok(f"🧠 Ricordato: «{ESC(fact)}»", say="Va bene, me lo ricordo.")
            if status == "exists":
                return _ok(f"🧠 Lo sapevo già: «{ESC(fact)}»", say="Lo sapevo già.")
            if status == "secret":
                return _ok("🔒 Password e numeri di carta non li metto nella memoria: per quelli c'è Passwords.", say="Le password non le salvo nella memoria: per quelle c'è Passwords.")
            if status == "full":
                return _err(f"La memoria è piena ({memory_service.MEMORY_MAX_FACTS} voci). Togline qualcuna: «dimentica …».")
            return _err("Dimmi cosa devo ricordare: «ricordati che …».")
        if _SHOW_MEMORY.match(text):
            facts = memory_service.read(force=True)
            if not facts:
                return _ok("🧠 Non mi hai ancora chiesto di ricordare nulla. Dimmi «ricordati che …».", say="Non mi hai ancora chiesto di ricordare nulla.")
            n = len(facts)
            return _ok(f"🧠 <b>Ricordo {n} {'cosa' if n == 1 else 'cose'}</b>\n" + "\n".join(f"• {ESC(f)}" for f in facts),
                       say=(f"Ricordo {'una cosa' if n == 1 else f'{n} cose'}: " + "; ".join(_speakable(f) for f in facts[:3]) + ("." if n <= 3 else f"; e altre {n - 3}.")))
        m = _FORGET.match(text)
        if m:
            found = memory_service.find(m.group(1), memory_service.read(force=True))
            if not found:
                return _err(f"Non ho in memoria niente che somigli a «{m.group(1).strip()}».")

            def forget(fact):
                def action():
                    gone = memory_service.remove(fact)
                    return _ok(f"🗑️ Dimenticato: «{ESC(fact)}»" if gone else "Non c'era più.", say="Fatto, l'ho dimenticato." if gone else "Non c'era più.")
                return action
            if len(found) == 1:
                return self._ask(s, f"Dimentico questo?\n<b>{ESC(found[0])}</b>", forget(found[0]), yes="Dimentica",
                                 say=f"Dimentico questo: {_speakable(found[0])}? Dimmi sì o no.")
            q = "Quale dimentico?\n" + "\n".join(f"{i + 1}. <b>{ESC(f)}</b>" for i, f in enumerate(found[:4]))
            return self._ask_choice(s, q, [(f, forget(f)) for f in found[:4]], say=f"Ne ho trovate {len(found[:4])}. Quale dimentico?")
        return None

    # ----- promemoria ------------------------------------------------------
    @staticmethod
    def _agenda_tasks(now):
        """I 7 promemoria più urgenti della lista TO DO e quanti sono in tutto (se Tasks non risponde: nessuno, senza bloccare l'agenda)."""
        try:
            return tasks_service.top(tasks_service.public(tasks_service.open_tasks(), now))
        except Exception:
            logger.debug("Promemoria non raggiungibili", exc_info=True)
            return [], 0

    @staticmethod
    def _task_lines(tasks, total=None) -> str:
        lines = [f"• {ESC(t['title'])}" + (f" <i>({ESC(t['label'])})</i>" if t.get("label") else "") for t in tasks]
        if total and total > len(tasks):
            lines.append(f"<i>… e altri {total - len(tasks)}</i>")
        return "\n".join(lines)

    @staticmethod
    def _say_tasks(tasks, total=None) -> str:
        n = total or len(tasks)
        names = [_speakable(t["title"]) for t in tasks[:3]]
        head = "E hai un promemoria: " if n == 1 else f"E hai {n} promemoria: "
        listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " e " + names[-1]
        return head + listed + ("." if n <= len(names) else f", e altri {n - len(names)}.")

    @staticmethod
    def _say_due(due, now) -> str:
        if due is None:
            return "senza scadenza"
        word = say_day(due.date(), now)
        return f"entro {word}" if (due.hour, due.minute) == (23, 59) else f"entro {word} {say_time(due)}"

    def _add_task(self, s, f, text, now):
        title = (f.title or "").strip()
        if not title:
            return _err("Dimmi cosa devo ricordarti.")
        due, note = tasks_service.compute_due(now, f.due_day, f.due_clock)
        t = tasks_service.create_task(title, due, f.task_list)
        when = f"⏰ <b>{ESC(tasks_service.due_label(due, now))}</b>" + (f" <i>({ESC(note)})</i>" if note else "") if due else "⏰ senza scadenza"
        say = f"Fatto. Ho aggiunto {_speakable(title)} nella lista {_speakable(t['list'])}, {self._say_due(due, now)}." + (f" Nota: {note}." if note else "")
        return _ok(f"☑️ Promemoria aggiunto in <b>{ESC(t['list'])}</b>:\n<b>{ESC(title)}</b>\n{when}", ["tasks"], say=say)

    def _get_tasks(self, s, f, text, now):
        tasks = tasks_service.open_tasks(f.task_list)
        if not tasks:
            return _ok("☑️ Non hai promemoria aperti. 🎉", say="Non hai promemoria aperti.")
        lines = [f"☑️ <b>Promemoria aperti</b> ({len(tasks)})"]
        for t in tasks[:15]:
            lab = tasks_service.due_label(t["due"], now)
            lines.append(f"• {ESC(t['title'])}" + (f" <i>({ESC(lab)})</i>" if lab else ""))
        if len(tasks) > 15:
            lines.append(f"<i>… e altri {len(tasks) - 15}</i>")
        n = len(tasks)
        names = [_speakable(t["title"]) for t in tasks[:3]]
        listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " e " + names[-1]
        say = (f"Hai un promemoria: {listed}." if n == 1 else f"Hai {n} promemoria: {listed}" + ("." if n <= 3 else f", e altri {n - 3}."))
        return _ok("\n".join(lines), say=say)

    def _complete_task(self, s, f, text, now):
        query = (f.title or "").strip()
        if not query:
            return _err("Dimmi quale promemoria vuoi spuntare.")
        tasks = tasks_service.open_tasks(f.task_list)
        exact, fuzzy = tasks_service.match(query, tasks)

        def spunta(t):                                           # azione: spunta e offre di annullare
            def action():
                tasks_service.set_done(t["_obj"], True)
                undo = self._ask(s, f"✅ Spuntato: <b>{ESC(t['title'])}</b>", lambda: self._reopen(t), yes="Annulla", no="Ok",
                                 say=f"Fatto, ho spuntato {_speakable(t['title'])}.", no_text="Va bene.", listen=False)
                undo.refresh = ["tasks"]
                return undo
            return action

        if len(exact) == 1:                                      # uno solo: lo spunto subito (si può annullare)
            return spunta(exact[0])()
        pool = exact or fuzzy
        if not pool:
            return _err(f"Non trovo nessun promemoria aperto che somigli a «{query}».")
        if len(pool) == 1:                                       # solo una somiglianza approssimativa: meglio chiedere
            t = pool[0]
            return self._ask(s, f"Intendi <b>{ESC(t['title'])}</b>?", spunta(t), yes="Sì, spunta",
                             say=f"Intendi {_speakable(t['title'])}? Dimmi sì o no.")
        opts = [(t["title"], spunta(t)) for t in pool[:4]]
        nomi = ", ".join(f"{['il primo', 'il secondo', 'il terzo', 'il quarto'][i]}: {_speakable(t['title'])}" for i, t in enumerate(pool[:4]))
        q = "Quale intendi?\n" + "\n".join(f"{i + 1}. <b>{ESC(t['title'])}</b>" for i, t in enumerate(pool[:4]))
        return self._ask_choice(s, q, opts, say=f"Ne ho trovati {len(pool[:4])}. Quale intendi? {nomi}.")

    @staticmethod
    def _reopen(t) -> Reply:
        tasks_service.set_done(t["_obj"], False)
        return _ok(f"↩️ Riaperto: <b>{ESC(t['title'])}</b>", ["tasks"], say=f"Va bene, ho riaperto {_speakable(t['title'])}.")

    # ----- note ------------------------------------------------------------
    def _add_note(self, s, f, text, now):
        title = f.title if f.title and f.title != "Senza titolo" else "Nota"
        content = f.description or text
        r = notes_service.save_note_to_nextcloud(title, content, f.raw_category)
        if not r.get("success"):
            return _err(f"Errore nel salvataggio della nota: {r.get('error')}")
        return _ok(f"📝 <b>Nota salvata</b>: {ESC(r['title'])} <i>({ESC(r.get('folder', 'Note'))})</i>\n{ESC(content[:600])}", ["notes"],
                   say=f"Nota salvata: {_speakable(r['title'])}.")

    def _get_notes(self, s, f, text, now):
        if f.title:
            content = notes_service.get_note_content(f.title, f.raw_category)
            if not content:
                return _err(f"Nota «{f.title}» non trovata.")
            return _ok(f"📄 <b>{ESC(f.title)}</b>\n{ESC(content[:3000])}")
        notes = notes_service.list_notes(f.raw_category)
        if not notes:
            return _ok("📭 Nessuna nota trovata.")
        return _ok("📝 <b>Le tue note</b>\n" + "\n".join(f"• {ESC(n['title'])}" for n in notes[:40])
                   + "\n<i>Chiedimi «leggimi la nota …» per aprirne una.</i>")

    # ----- finanze ---------------------------------------------------------
    def _need_finance(self):
        try:
            return _finance()
        except ImportError:
            return None

    def _expense(self, s, f, text, now):
        fin = self._need_finance()
        if fin is None:
            return _err("Le finanze non sono configurate su questo server.")
        out = _run(fin.add_expense(self.owner_id, f.amount, f.finance_category, f.title, f.start_str))
        failed = out.lstrip().startswith("❌")
        voce = f.title or f.finance_category or "la spesa"
        return _ok(md_to_html(out), [] if failed else ["finance"],
                   say=to_plain(md_to_html(out), 160) if failed else f"Fatto, ho registrato {say_amount(f.amount)} per {_speakable(voce)}.")

    def _income(self, s, f, text, now):
        fin = self._need_finance()
        if fin is None:
            return _err("Le finanze non sono configurate su questo server.")
        out = _run(fin.add_income(self.owner_id, f.amount, f.finance_category, f.title, f.start_str))
        failed = out.lstrip().startswith("❌")
        return _ok(md_to_html(out), [] if failed else ["finance"],
                   say=to_plain(md_to_html(out), 160) if failed else f"Fatto, ho registrato un'entrata di {say_amount(f.amount)}.")

    def _finance_summary(self, s, f, text, now):
        fin = self._need_finance()
        if fin is None:
            return _err("Le finanze non sono configurate su questo server.")
        out = _run(fin.get_summary(self.owner_id, f.period, f.finance_category, f.start_str, f.end_str))
        return _ok(md_to_html(out))

    # ----- news e server ---------------------------------------------------
    def _news(self, s, f, text, now):
        groups, chosen = news_service.collect_latest(f.news_topic)
        sections = digest.build_latest(groups, chosen, f.news_topic)
        if not sections:
            quale = f" su «{ESC(f.news_topic)}»" if chosen else ""
            return _ok(f"📰 Non ho trovato notizie{quale}. Controlla i feed in Nextcloud News.")
        return _ok("\n\n".join(sections))

    def _server(self, s, f, text, now):
        return _ok(monitor_service.get_monitor().server_report(), ["server"])
