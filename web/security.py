"""Accesso alla web app: identità Tailscale (consigliata) oppure password. Difese di base per un servizio personale."""
import hashlib
import hmac
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field

COOKIE_NAME = "edith_session"
COOKIE_TTL = 30 * 24 * 3600
LOGIN_MAX_FAILS = 5
LOGIN_WINDOW = 300


@dataclass
class WebSettings:
    allowed_logins: list = field(default_factory=list)     # minuscolo
    password: str = ""
    secret: bytes = b""
    assistant_name: str = "EDITH"
    owner_name: str = ""
    owner_nick: str = ""             # come ti chiama la voce (es. «Mario»); se vuoto si usa owner_name
    trust_header: bool = True        # l'header Tailscale vale solo se il servizio ascolta in locale (vedi server.py)
    quote_packs: list = field(default_factory=list)   # pacchetti di frasi scelti (vuoto = tutti)

    @property
    def configured(self) -> bool:
        return bool(self.allowed_logins or self.password)


def make_secret(configured: str, bot_token: str) -> bytes:
    """Chiave per firmare i cookie: quella scelta nel .env, altrimenti derivata dal token del bot (segreto e stabile)."""
    return (configured or hashlib.sha256(f"edith-web|{bot_token}".encode()).hexdigest()).encode()


def make_cookie(st: WebSettings, now: float = None) -> str:
    now = time.time() if now is None else now
    payload = f"{int(now + COOKIE_TTL)}.{secrets.token_hex(8)}"
    return f"{payload}.{hmac.new(st.secret, payload.encode(), hashlib.sha256).hexdigest()}"


def check_cookie(st: WebSettings, value: str, now: float = None) -> bool:
    if not value or not st.password:          # i cookie esistono solo in modalità password
        return False
    now = time.time() if now is None else now
    try:
        exp, nonce, sig = value.split(".")
        expected = hmac.new(st.secret, f"{exp}.{nonce}".encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected) and int(exp) > now
    except (ValueError, TypeError):
        return False


def tailscale_login(request) -> str:
    return (request.headers.get("tailscale-user-login") or "").strip().lower()


def identify(request, st: WebSettings):
    """Ritorna una stringa che identifica l'utente autorizzato, oppure None."""
    login = tailscale_login(request)
    if login and st.trust_header and login in st.allowed_logins:
        return login
    if check_cookie(st, request.cookies.get(COOKIE_NAME, "")):
        return "password"
    return None


def password_ok(st: WebSettings, candidate: str) -> bool:
    return bool(st.password) and hmac.compare_digest(st.password.encode(), (candidate or "").encode())


def cross_site(request) -> bool:
    """True se il browser dichiara che la richiesta arriva da un altro sito (difesa contro CSRF)."""
    return request.headers.get("sec-fetch-site", "").lower() in ("cross-site", "same-site")


class LoginLimiter:
    """Dopo 5 password sbagliate in 5 minuti blocca i tentativi per il resto della finestra."""
    def __init__(self):
        self.fails = deque()
        self.lock = threading.Lock()

    def _prune(self, now):
        while self.fails and now - self.fails[0] > LOGIN_WINDOW:
            self.fails.popleft()

    def allowed(self, now: float = None) -> bool:
        now = time.time() if now is None else now
        with self.lock:
            self._prune(now)
            return len(self.fails) < LOGIN_MAX_FAILS

    def failed(self, now: float = None):
        now = time.time() if now is None else now
        with self.lock:
            self._prune(now)
            self.fails.append(now)

    def reset(self):
        with self.lock:
            self.fails.clear()


class RateLimiter:
    """Al massimo `limit` richieste ogni `window` secondi, in totale (è un servizio personale: protegge la quota di Gemini)."""
    def __init__(self, limit: int = 30, window: float = 60.0):
        self.limit, self.window = limit, window
        self.hits = deque()
        self.lock = threading.Lock()

    def hit(self, now: float = None) -> bool:
        now = time.time() if now is None else now
        with self.lock:
            while self.hits and now - self.hits[0] > self.window:
                self.hits.popleft()
            if len(self.hits) >= self.limit:
                return False
            self.hits.append(now)
            return True
