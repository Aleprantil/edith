"""
Memoria di EDITH: una nota di Nextcloud («EDITH Memoria»), una cosa da ricordare per riga.

  - La rilegge da sola (una richiesta a Nextcloud al minuto al massimo) e ne passa le righe a Gemini come DATI su di te,
    per capire chi è «Marco» o cosa intendi con «in università». Non sono istruzioni.
  - Impara solo quello che le chiedi: «ricordati che …». Niente che somigli a password o numeri di carta.
  - Puoi modificarla a mano da Nextcloud: ogni riga che comincia con «- » è una voce; il resto (titoli, spiegazioni) è ignorato.
"""
import logging
import re
import threading
import time

from config import MEMORY_MAX_FACTS, MEMORY_NOTE_TITLE
from services import notes_service as ns
from services.notes_service import NotesError

logger = logging.getLogger(__name__)

HEADER = ("# EDITH Memoria\n\nUna cosa da ricordare per riga (comincia con «- »). Puoi modificare questa nota da Nextcloud: "
          "EDITH la rilegge da sola. Non metterci password.\n\n")
MAX_FACT = 220                  # caratteri per voce
MAX_CHARS = 2500                # caratteri passati a Gemini in tutto
CACHE_TTL = 60                  # secondi
FAIL_TTL = 20                   # se Nextcloud non risponde, riprovo dopo 20 secondi (nel frattempo vale l'ultima copia buona)

_lock = threading.Lock()
_cache = {"at": 0.0, "ttl": 0.0, "facts": None}

_SECRET = re.compile(r"password|passw|parola\s+d.?ordine|\bpin\b|\bcvv\b|\biban\b|carta\s+di\s+credito|codice\s+segreto|\d[\d .-]{10,}\d", re.I)


def looks_secret(text: str) -> bool:
    return bool(_SECRET.search(text or ""))


def parse(text: str) -> list:
    """Le voci di una nota: SOLO le righe che cominciano con «- », «* » o «• ». Titoli, spiegazioni e righe vuote sono commenti e vengono ignorati."""
    facts, seen = [], set()
    for line in (text or "").splitlines():
        s = line.strip()
        if re.match(r"^\*?creata il", s, re.I):
            continue
        m = re.match(r"^[-*•]\s*(\S.*)$", s)
        if not m:
            continue
        s = re.sub(r"[\x00-\x1f]", " ", m.group(1))[:MAX_FACT].strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            facts.append(s)
    return facts[:MEMORY_MAX_FACTS]


def invalidate():
    with _lock:
        _cache.update(at=0.0, ttl=0.0)


def read(force: bool = False) -> list:
    """Le voci in memoria. Se Nextcloud non risponde ritorna l'ultima copia buona (o nulla): non solleva mai errori."""
    with _lock:
        fresh = _cache["facts"] is not None and time.time() - _cache["at"] < _cache["ttl"]
        if fresh and not force:
            return list(_cache["facts"])
        stale = list(_cache["facts"] or [])
    try:
        text = ns.read_note_exact(MEMORY_NOTE_TITLE)
    except Exception as e:                       # rete, credenziali, nota illeggibile: la memoria non deve mai bloccare un comando
        logger.warning("Memoria non raggiungibile: %s", e)
        with _lock:
            _cache.update(at=time.time(), ttl=FAIL_TTL, facts=stale)
        return stale
    facts = parse(text or "")
    with _lock:
        _cache.update(at=time.time(), ttl=CACHE_TTL, facts=facts)
    return list(facts)


def prompt_block() -> str:
    """Le voci in forma da aggiungere al prompt (vuota se non c'è nulla)."""
    out, used = [], 0
    for f in read():
        if used + len(f) > MAX_CHARS:
            break
        out.append(f"- {f}")
        used += len(f)
    return "\n".join(out)


def _raw() -> str:
    return ns.read_note_exact(MEMORY_NOTE_TITLE) or ""


def add(fact: str):
    """('added' | 'exists' | 'secret' | 'full' | 'empty', voce). Solleva NotesError se Nextcloud non risponde."""
    fact = re.sub(r"\s+", " ", (fact or "")).strip().rstrip(".").strip()[:MAX_FACT]
    if not fact:
        return "empty", ""
    if looks_secret(fact):
        return "secret", fact
    current = read(force=True)
    if fact.lower() in {f.lower() for f in current}:
        return "exists", fact
    if len(current) >= MEMORY_MAX_FACTS:
        return "full", fact
    raw = _raw()
    body = (raw if raw.strip() else HEADER)
    body = body + ("" if body.endswith("\n") else "\n") + f"- {fact}\n"
    res = ns.write_note_raw(MEMORY_NOTE_TITLE, body)
    if not res.get("success"):
        raise NotesError(f"Non sono riuscito a salvare la memoria ({res.get('error')})")
    invalidate()
    return "added", fact


def remove(fact: str) -> bool:
    """Toglie quella voce (la prima uguale). False se non c'era più."""
    raw, target, done, kept = _raw(), fact.strip().lower(), False, []
    for line in raw.splitlines():
        s = re.sub(r"^[-*•]\s*", "", line.strip()).strip()
        if not done and s and s.lower() == target:
            done = True
            continue
        kept.append(line)
    if not done:
        return False
    res = ns.write_note_raw(MEMORY_NOTE_TITLE, "\n".join(kept).rstrip("\n") + "\n")
    if not res.get("success"):
        raise NotesError(f"Non sono riuscito a salvare la memoria ({res.get('error')})")
    invalidate()
    return True


def _tokens(s: str) -> list:
    return re.findall(r"\w+", (s or "").lower())


def find(query: str, facts: list = None) -> list:
    """Le voci che contengono tutte le parole cercate; se nessuna, quelle che ne contengono almeno il 60%."""
    facts = read() if facts is None else facts
    q = [w for w in _tokens(query) if w not in {"il", "lo", "la", "le", "un", "una", "di", "del", "che", "è", "e", "a"}] or _tokens(query)
    if not q:
        return []
    exact, fuzzy = [], []
    for f in facts:
        words = set(_tokens(f))
        hit = sum(1 for w in q if w in words)
        if hit == len(q):
            exact.append(f)
        elif hit / len(q) >= 0.6:
            fuzzy.append((hit / len(q), f))
    return exact or [f for _, f in sorted(fuzzy, key=lambda x: -x[0])]
