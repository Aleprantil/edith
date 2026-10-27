"""
Logica dell'isola di EDITH, senza grafica: si prova con i test anche dove GTK non c'è.

- Client: parla con le API che EDITH ha già (/api/assistant, /api/confirm, /api/cards/*). Nessuna logica duplicata.
- to_pango: trasforma l'HTML (già ripulito) delle risposte nel markup di GTK, accettando solo grassetto, corsivo, codice, link e a capo.
- History: la cronologia locale della chat (solo su questo PC, file con permessi 600).
- Comandi locali: un socket Unix privato con cui Waybar e Hyprland dicono all'isola «apri», «chiudi», «chiedi».
- Riepilogo: prossimo impegno, promemoria scaduti, meteo e frase del giorno, con le stesse regole della pagina e del widget.
Solo libreria standard.
"""
import concurrent.futures
import hashlib
import html
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

DEFAULT_URL = "https://edith.your-tailnet.ts.net:8443"
HOME = Path.home()
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "edith"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", HOME / ".cache")) / "edith"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", HOME / ".local" / "share")) / "edith"
COMMANDS = ("toggle", "show", "hide", "ask", "refresh", "quit", "listen", "stop", "talk")
ASK_TIMEOUT = 80                                   # come la pagina: Nextcloud e Gemini a volte sono lenti
CARD_TIMEOUT = 5


# ---------------------------------------------------------------------------
# Impostazioni
# ---------------------------------------------------------------------------

def settings() -> dict:
    """~/.config/edith/edith.conf (o quello del widget, waybar.conf), poi le variabili d'ambiente."""
    conf = {"EDITH_URL": DEFAULT_URL, "EDITH_ISLAND_POSITION": "center", "EDITH_ISLAND_WIDTH": "440", "EDITH_SIGNAL": "8", "EDITH_NICKNAME": "",
            "EDITH_ISLAND_VOICE": "auto",                                       # auto: risponde a voce quando le parli; always; off
            "EDITH_VOICE_MODEL": str(DATA_DIR / "voices" / "it_IT-paola-medium.onnx"),
            "EDITH_PIPER_PYTHON": str(DATA_DIR / "piper" / "bin" / "python"),
            "EDITH_VOICE_SPEED": "1.0"}
    for name in ("waybar.conf", "edith.conf"):                      # edith.conf vince sul file del widget
        try:
            for line in (CONFIG_DIR / name).read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.split("=", 1)
                    conf[k.strip()] = v.strip().strip('"\'')
        except OSError:
            pass
    for k in list(conf):
        if os.environ.get(k):
            conf[k] = os.environ[k]
    conf["EDITH_URL"] = conf["EDITH_URL"].rstrip("/")
    if conf["EDITH_ISLAND_POSITION"] not in ("center", "left", "right"):
        conf["EDITH_ISLAND_POSITION"] = "center"
    try:
        conf["EDITH_ISLAND_WIDTH"] = str(min(720, max(340, int(conf["EDITH_ISLAND_WIDTH"]))))
    except ValueError:
        conf["EDITH_ISLAND_WIDTH"] = "440"
    if conf["EDITH_ISLAND_VOICE"] not in ("auto", "always", "off"):
        conf["EDITH_ISLAND_VOICE"] = "auto"
    try:
        conf["EDITH_VOICE_SPEED"] = str(min(2.0, max(0.5, float(conf["EDITH_VOICE_SPEED"].replace(",", ".")))))
    except ValueError:
        conf["EDITH_VOICE_SPEED"] = "1.0"
    for k in ("EDITH_VOICE_MODEL", "EDITH_PIPER_PYTHON"):
        conf[k] = os.path.expanduser(conf[k])
    return conf


def session_id() -> str:
    """Un identificativo stabile per questo PC: EDITH ricorda il filo della conversazione (es. «sì, quello»)."""
    host = re.sub(r"[^A-Za-z0-9_-]", "-", socket.gethostname())[:40] or "pc"
    return f"island-{host}".ljust(12, "x")[:64]


# ---------------------------------------------------------------------------
# Rete
# ---------------------------------------------------------------------------

class EdithError(Exception):
    """Messaggio già pronto da mostrare."""


class AuthError(EdithError):
    pass


class OfflineError(EdithError):
    pass


class Client:
    def __init__(self, base: str, session: str = None, opener=None):
        self.base, self.session = base.rstrip("/"), session or session_id()
        self._open = opener or urllib.request.urlopen

    def _request(self, path: str, body: dict = None, timeout: float = CARD_TIMEOUT, wav: bytes = None):
        headers = {"Accept": "application/json", "User-Agent": "edith-island"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers.update({"Content-Type": "application/json", "X-Requested-With": "edith"})
        elif wav is not None:                                                # l'audio va così com'è, come lo manda la pagina
            data = wav
            headers.update({"Content-Type": "audio/wav", "X-Requested-With": "edith", "X-Session": self.session})
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method="POST" if data is not None else "GET")
        try:
            with self._open(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise AuthError("Questo PC non è riconosciuto da EDITH: deve essere su Tailscale con il tuo account "
                                "(o il suo accesso va aggiunto a WEB_ALLOWED_LOGINS).") from e
            if e.code == 429:
                raise EdithError("Troppe richieste in poco tempo: aspetta qualche secondo.") from e
            raise EdithError(f"EDITH ha risposto con un errore ({e.code}).") from e
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
            raise OfflineError("EDITH non risponde: controlla che Tailscale sia acceso e che il server sia raggiungibile.") from e
        except ValueError as e:
            raise EdithError("EDITH ha mandato una risposta che non capisco.") from e

    def ask(self, text: str) -> dict:
        text = " ".join((text or "").split())[:500]
        if not text:
            raise EdithError("Scrivi qualcosa.")
        return self._request("/api/assistant", {"text": text, "session": self.session}, ASK_TIMEOUT)

    def voice(self, wav: bytes) -> dict:
        """Quello che hai detto: EDITH lo trascrive e risponde; «heard» è ciò che ha capito, «speak» la frase da dire."""
        if not wav:
            raise EdithError("Non ho registrato niente.")
        return self._request("/api/voice", timeout=ASK_TIMEOUT, wav=wav)

    def confirm(self, token: str, yes: bool) -> dict:
        return self._request("/api/confirm", {"session": self.session, "token": token, "yes": bool(yes)}, ASK_TIMEOUT)

    def cards(self, names=("today", "tasks", "server")) -> dict:
        """Le schede in parallelo; una che fallisce non nasconde le altre. AuthError passa; se falliscono tutte, OfflineError."""
        out, errors = {}, []

        def one(n):
            r = self._request(f"/api/cards/{n}")
            return n, (r.get("data") if r.get("ok") else None)
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(names)) as pool:
            for f in [pool.submit(one, n) for n in names]:
                try:
                    n, data = f.result()
                    out[n] = data
                except AuthError:
                    raise
                except EdithError as e:
                    errors.append(e)
        if not any(out.values()):
            raise errors[0] if errors else OfflineError("Nessun dato da EDITH.")
        return out

    def quotes(self) -> list:
        """Le frasi di EDITH, rilette al massimo una volta al giorno (copia in ~/.cache/edith)."""
        cache = CACHE_DIR / "frasi.json"
        try:
            c = json.loads(cache.read_text(encoding="utf-8"))
            if time.time() - c.get("at", 0) < 86400:
                return c.get("frasi", [])
        except (OSError, ValueError):
            c = {}
        try:
            frasi = [f for f in self._request("/static/frasi.json").get("frasi", []) if isinstance(f, dict) and isinstance(f.get("t"), str)]
            _write_private(cache, {"at": time.time(), "frasi": frasi})
            return frasi
        except EdithError:
            return c.get("frasi", [])


def _write_private(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Testo: dalle risposte di EDITH al markup di GTK
# ---------------------------------------------------------------------------

class _ToPango(HTMLParser):
    TAGS = {"b": "b", "strong": "b", "i": "i", "em": "i", "code": "tt"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.stack = [], []

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            self.out.append("\n")
        elif tag in self.TAGS:
            self.out.append(f"<{self.TAGS[tag]}>")
            self.stack.append(self.TAGS[tag])
        elif tag == "a":
            href = (dict(attrs).get("href") or "").strip()
            if "a" not in self.stack and re.match(r"^https?://[^\s\"'<>]+$", href, re.I):   # niente link dentro un link
                self.out.append(f'<a href="{html.escape(href, quote=True)}">')
                self.stack.append("a")
            else:
                self.stack.append(None)                          # link non sicuro: resta solo il testo

    def handle_startendtag(self, tag, attrs):
        if tag == "br":
            self.out.append("\n")

    def handle_endtag(self, tag):
        want = self.TAGS.get(tag, "a" if tag == "a" else "")
        if not want:
            return
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i] == want or (tag == "a" and self.stack[i] is None):
                for t in reversed(self.stack[i:]):              # chiudo anche quelli rimasti aperti dentro, nell'ordine giusto
                    if t:
                        self.out.append(f"</{t}>")
                del self.stack[i:]
                return

    def handle_data(self, data):
        self.out.append(html.escape(data, quote=False))

    def result(self) -> str:
        for t in reversed(self.stack):
            if t:
                self.out.append(f"</{t}>")
        self.stack = []
        return re.sub(r"\n{3,}", "\n\n", "".join(self.out)).strip()


def to_pango(html_text: str) -> str:
    p = _ToPango()
    try:
        p.feed(html_text or "")
        p.close()
        return p.result()
    except Exception:
        return html.escape(plain(html_text), quote=False)


def plain(html_text: str) -> str:
    t = re.sub(r"<br\s*/?>", "\n", html_text or "")
    return html.unescape(re.sub(r"<[^>]+>", "", t)).strip()


# ---------------------------------------------------------------------------
# Cronologia locale
# ---------------------------------------------------------------------------

class History:
    """Gli ultimi messaggi, solo su questo PC. {"who": "me"|"edith"|"error", "text": ..., "ts": ...}"""

    def __init__(self, path: Path = None, cap: int = 120):
        self.path, self.cap = path or (DATA_DIR / "island-history.json"), cap
        self.items = self._load()

    def _load(self) -> list:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        ok = [m for m in data if isinstance(m, dict) and m.get("who") in ("me", "edith", "error") and isinstance(m.get("text"), str)] if isinstance(data, list) else []
        return ok[-self.cap:]

    def add(self, who: str, text: str) -> dict:
        m = {"who": who, "text": text[:6000], "ts": round(time.time())}
        self.items = (self.items + [m])[-self.cap:]
        self.save()
        return m

    def last_question(self):
        return next((m["text"] for m in reversed(self.items) if m["who"] == "me"), None)

    def clear(self) -> None:
        self.items = []
        self.save()

    def save(self) -> None:
        try:
            _write_private(self.path, self.items)
        except OSError:
            pass                                                 # disco pieno o cartella bloccata: la chat continua lo stesso


# ---------------------------------------------------------------------------
# Comandi locali (Waybar, scorciatoie di Hyprland)
# ---------------------------------------------------------------------------

def socket_path() -> Path:
    run = os.environ.get("XDG_RUNTIME_DIR")
    return Path(run) / "edith-island.sock" if run and os.path.isdir(run) else Path(f"/tmp/edith-island-{os.getuid()}.sock")


def send_command(cmd: str, path: Path = None, timeout: float = 0.6) -> bool:
    """True se un'isola già aperta ha ricevuto il comando."""
    if cmd not in COMMANDS:
        raise ValueError(cmd)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(path or socket_path()))
        s.sendall(cmd.encode() + b"\n")
        return s.recv(8).startswith(b"ok")
    except OSError:
        return False
    finally:
        s.close()


class CommandServer:
    """Ascolta i comandi su un socket privato (permessi 600). Accetta solo parole note: niente testo eseguito."""

    def __init__(self, handler, path: Path = None):
        self.handler, self.path = handler, path or socket_path()
        self.sock, self._stop = None, threading.Event()

    def start(self) -> bool:
        if self.path.exists():
            if send_command("refresh", self.path, 0.3):
                return False                                     # c'è già un'isola aperta
            try:
                self.path.unlink()                               # resto di un'isola chiusa male
            except OSError:
                pass
        old = os.umask(0o177)
        try:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.bind(str(self.path))
        finally:
            os.umask(old)
        os.chmod(self.path, 0o600)
        self.sock.listen(4)
        self.sock.settimeout(0.5)
        threading.Thread(target=self._loop, name="edith-island-ipc", daemon=True).start()
        return True

    def _loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                try:
                    conn.settimeout(0.5)
                    cmd = conn.recv(32).decode("ascii", "ignore").strip()
                    ok = cmd in COMMANDS
                    if ok:
                        self.handler(cmd)
                    conn.sendall(b"ok\n" if ok else b"no\n")
                except OSError:
                    pass

    def stop(self):
        self._stop.set()
        try:
            if self.sock:
                self.sock.close()
        finally:
            try:
                self.path.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Riepilogo: stesse regole della pagina e del widget di Waybar
# ---------------------------------------------------------------------------

def in_label(mins: int) -> str:
    if mins <= 0:
        return "ora"
    if mins < 60:
        return f"tra {mins} min"
    h, m = divmod(mins, 60)
    return f"tra {h} h" + (f" {m} min" if m else "")


def _dt(s):
    try:
        return datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


def next_event(today: dict, now: datetime):
    if not today:
        return None
    timed = [(e, s, f) for e in today.get("events") or [] for s, f in [(_dt(e.get("start_iso")), _dt(e.get("end_iso")))] if s and f]
    timed.sort(key=lambda x: x[1])
    for e, s, f in timed:
        if s <= now < f:
            return {"when": "in corso", "title": e.get("title"), "loc": e.get("location"), "extra": f"fino alle {f:%H:%M}", "now": True}
    for e, s, _f in timed:
        if s > now:
            mins = int((s - now).total_seconds() // 60) + 1
            return {"when": in_label(mins) if mins < 360 else f"alle {s:%H:%M}", "title": e.get("title"), "loc": e.get("location"), "mins": mins}
    for g in today.get("upcoming") or []:
        evs = sorted(((e, s) for e in g.get("events") or [] for s in [_dt(e.get("start_iso"))] if s), key=lambda x: x[1])
        if evs:
            e, s = evs[0]
            day = "domani" if g.get("label") == "Domani" else str(g.get("label", "")).split(" ")[0]
            return {"when": f"{day} {s:%H:%M}", "title": e.get("title"), "loc": e.get("location")}
    return None


def server_bad(server: dict) -> bool:
    if not server:
        return False
    nc = server.get("nextcloud") or {}
    return bool(server.get("alerts")) or nc.get("ok") is False or any(not m.get("mounted") or m.get("ro") for m in server.get("mounts") or [])


def flags(data: dict, now: datetime) -> dict:
    h, dow, mo = now.hour, now.weekday(), now.month
    f = {"mattina": 5 <= h < 12, "pomeriggio": 12 <= h < 18, "sera": 18 <= h < 23, "notte": h >= 23 or h < 5,
         "lunedi": dow == 0, "martedi": dow == 1, "mercoledi": dow == 2, "giovedi": dow == 3, "venerdi": dow == 4,
         "sabato": dow == 5, "domenica": dow == 6, "weekend": dow >= 5, "inverno": mo in (12, 1, 2, 3), "estate": mo in (6, 7, 8, 9)}
    t, tk, sv = data.get("today"), data.get("tasks"), data.get("server")
    if t:
        w = t.get("weather") or {}
        lab = str(w.get("label", "")).lower()
        evs = t.get("events") or []
        dom = next((g for g in t.get("upcoming") or [] if g.get("label") == "Domani"), {})
        text = lambda e: f"{e.get('title', '')} {e.get('calendar', '')}"
        f.update(pioggia=bool(re.search(r"piogg|rovesc|temporal|piovigg", lab)), neve=bool(re.search(r"neve|nevisc", lab)), sereno="sereno" in lab,
                 freddo=bool(w) and (w.get("tmax") or 99) <= 8, caldo=bool(w) and (w.get("tmax") or -99) >= 28,
                 esame=any(re.search(r"esam", e.get("title", ""), re.I) for e in evs + (dom.get("events") or [])),
                 allenamento=any(re.search(r"allenament|calcio|partita", text(e), re.I) for e in evs),
                 lezione=any(re.search(r"lezion|\blez\b|\blab\b|universit", text(e), re.I) for e in evs),
                 giornata_libera=not evs, giornata_piena=len(evs) >= 5)
    if tk:
        f.update(scaduti=(tk.get("overdue") or 0) > 0, tutto_fatto=(tk.get("total") or 0) == 0)
    if sv:
        bad = server_bad(sv)
        f.update(server_avviso=bad, server_ok=not bad)
    return f


def pick_quote(frasi: list, data: dict, now: datetime, seed: int = None):
    """Una frase adatta al momento (le condizioni «c» devono essere tutte vere), altrimenti una generale."""
    if not frasi:
        return None
    f = flags(data, now)
    ctx = [q for q in frasi if q.get("c") and all(f.get(k) for k in q["c"])]
    gen = [q for q in frasi if not q.get("c")]
    n = seed if seed is not None else int(hashlib.sha256(f"{now:%Y-%m-%d %H}".encode()).hexdigest(), 16)
    pool = ctx if ctx and (n % 5 < 3 or not gen) else (gen or frasi)
    return pool[n % len(pool)]["t"]


def summary(data: dict, now: datetime) -> dict:
    """Ciò che la striscia «Adesso» mostra, già pronto: niente logica nella grafica."""
    today, tasks, server = data.get("today") or {}, data.get("tasks") or {}, data.get("server") or {}
    w = (today.get("weather_days") or [None])[0] or today.get("weather")
    weather = f"{w.get('emoji', '')} {w.get('tmin')}–{w.get('tmax')}°" if w and w.get("tmax") is not None else ""
    return {"next": next_event(today, now), "overdue": tasks.get("overdue") or 0, "open": tasks.get("total") or 0,
            "weather": weather.strip(), "status": "alert" if server_bad(server) else "ok"}


GREETINGS_EXTRA = ("Eccomi.", "Pronta.", "Dimmi tutto.", "Che si fa?", "Tutto sotto controllo.", "Sono qui.", "Al tuo servizio.", "Sistemi in ordine.")


def greeting(now: datetime, nick: str = "", n: int = None) -> str:
    """Il saluto del personaggio: dipende dall'ora, con una piccola variante."""
    h = now.hour
    name = f", {nick}" if nick else ""                                  # senza soprannome: «Buongiorno!»
    if h >= 23 or h < 5:
        first = f"Ancora sveglio{name}?"
    elif h < 12:
        first = f"Buongiorno{name}!"
    elif h < 18:
        first = f"Ciao{name}!"
    else:
        first = f"Buonasera{name}!"
    n = int(time.time() * 1000) if n is None else n
    return f"{first} {GREETINGS_EXTRA[n % len(GREETINGS_EXTRA)]}"


# ---------------------------------------------------------------------------
# Il personaggio che parla: testo a comparsa, riepilogo, linea della giornata
# ---------------------------------------------------------------------------

_TOKEN = re.compile(r"<[^>]+>|&[#\w]+;|[^<&]", re.S)


def visible_len(markup: str) -> int:
    """Quanti caratteri si vedono (i tag non contano, un'entità come &amp; conta uno)."""
    return sum(1 for t in _TOKEN.findall(markup or "") if not t.startswith("<"))


def markup_prefix(markup: str, n: int) -> str:
    """I primi n caratteri visibili, con i tag ancora aperti chiusi nell'ordine giusto: markup sempre valido."""
    out, stack, count = [], [], 0
    for tok in _TOKEN.findall(markup or ""):
        if tok.startswith("</"):
            out.append(tok)
            if stack:
                stack.pop()
        elif tok.startswith("<"):
            out.append(tok)
            m = re.match(r"<\s*([A-Za-z]+)", tok)
            if m and not tok.endswith("/>"):
                stack.append(m.group(1))
        else:
            if count >= n:
                break
            out.append(tok)
            count += 1
    return "".join(out) + "".join(f"</{t}>" for t in reversed(stack))


def is_agenda(html_text: str) -> bool:
    """Le risposte sull'agenda cominciano con 📅: lì il fumetto mostra anche la linea della giornata."""
    return plain(html_text).lstrip().startswith("📅")


def idle_line(s: dict) -> str:
    """Quello che EDITH dice quando apri l'isola, in markup GTK (testo già protetto)."""
    e = lambda x: html.escape(str(x or ""), quote=False)
    n, parts = s.get("next"), []
    if n:
        title = e(n.get("title") or "un impegno")
        if n.get("now"):
            parts.append(f"Adesso sei a <b>{title}</b>, {e(n.get('extra'))}.")
        elif str(n.get("when", "")).startswith("alle "):
            parts.append(f"Il prossimo impegno è <b>{title}</b> {e(n['when'])}.")
        else:
            parts.append(f"Il prossimo impegno è <b>{title}</b>, {e(n.get('when'))}.")
    else:
        parts.append("Nessun impegno in vista: giornata libera.")
    if s.get("overdue"):
        parts.append("Hai un promemoria scaduto." if s["overdue"] == 1 else f"Hai {s['overdue']} promemoria scaduti.")
    return " ".join(parts)


def timeline(today: dict, now: datetime):
    """Gli impegni con orario di oggi come punti su una linea: done, now (in corso), next, later. None se non ce ne sono."""
    evs = sorted(((e, s, f) for e in (today or {}).get("events") or [] for s, f in [(_dt(e.get("start_iso")), _dt(e.get("end_iso")))] if s and f),
                 key=lambda x: x[1])
    if not evs:
        return None
    lo, hi = evs[0][1], max(f for _e, _s, f in evs)
    span = max((hi - lo).total_seconds(), 1.0)
    points, highlighted = [], False
    for e, s, f in evs:
        if f <= now:
            st = "done"
        elif s <= now < f:
            st, highlighted = "now", True
        elif not highlighted:
            st, highlighted = "next", True
        else:
            st = "later"
        x = 0.5 if len(evs) == 1 else (s - lo).total_seconds() / span
        points.append({"label": f"{s:%H:%M}", "x": round(x, 4), "state": st, "title": e.get("title") or ""})
    if now <= lo:
        now_x = 0.0
    elif now >= hi:
        now_x = 1.0
    else:
        now_x = 0.5 if len(evs) == 1 else (now - lo).total_seconds() / span
    return {"points": points, "now_x": round(now_x, 4)}


def history_pairs(items: list) -> list:
    """La cronologia come coppie domanda → risposta, dalla più vecchia alla più recente."""
    pairs, q = [], None
    for m in items:
        if m.get("who") == "me":
            q = m.get("text")
        elif m.get("who") in ("edith", "error"):
            pairs.append({"q": q, "a": m.get("text", ""), "who": m["who"]})
            q = None
    return pairs
