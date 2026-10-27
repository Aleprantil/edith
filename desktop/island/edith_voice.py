"""
La voce dell'isola di EDITH, senza grafica (si prova con i test):

- Recorder: registra dal microfono mentre tieni premuto (parecord di PipeWire/PulseAudio, oppure arecord), con il livello
  della voce in diretta per far reagire il personaggio. Ne esce un WAV a 16 kHz mono, quello che la rotta /api/voice di EDITH vuole.
- Speaker: dà voce alle risposte con Piper, in locale sul PC. La voce resta caricata in un processo a parte
  (edith_tts_worker.py, dentro l'ambiente di Piper), così ogni frase parte subito. Poi la suona con pw-play, paplay o aplay.
- envelope: dall'audio vero ricava quanto aprire la bocca, istante per istante.
Solo libreria standard.
"""
import array
import io
import json
import math
import os
import queue
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import unicodedata
import wave
from pathlib import Path

RATE = 16000
MAX_SECONDS = 25.0                    # la rotta della voce accetta fino a 28 secondi: un po' di margine
MIN_SECONDS = 0.35
CHUNK = 3200                          # 0,1 secondi di audio

RECORDERS = (
    ("parecord", ["parecord", "--raw", "--rate=16000", "--channels=1", "--format=s16le", "--latency-msec=40"]),
    ("arecord", ["arecord", "-q", "-t", "raw", "-f", "S16_LE", "-r", "16000", "-c", "1"]),
)
PLAYERS = (("pw-play", ["pw-play"]), ("paplay", ["paplay"]), ("aplay", ["aplay", "-q"]))
HERE = Path(__file__).resolve().parent


class VoiceError(Exception):
    """Messaggio già pronto da mostrare."""


def find_tool(options, env_var: str = None, which=shutil.which):
    """Il primo programma disponibile; una variabile d'ambiente può indicarne uno preciso (comando completo)."""
    if env_var and os.environ.get(env_var):
        return shlex.split(os.environ[env_var])
    return next((list(cmd) for name, cmd in options if which(name)), None)


# ---------------------------------------------------------------------------
# Audio: WAV, livello, inviluppo
# ---------------------------------------------------------------------------

def build_wav(pcm: bytes, rate: int = RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm[: len(pcm) - (len(pcm) % 2)])
    return buf.getvalue()


def wav_seconds(data: bytes):
    try:
        with wave.open(io.BytesIO(data)) as w:
            return w.getnframes() / float(w.getframerate())
    except (wave.Error, EOFError, ZeroDivisionError):
        return None


def rms_level(pcm: bytes) -> float:
    """Il volume di un pezzo di audio, da 0 a 1, già «percettivo» (la voce normale sta verso metà)."""
    samples = array.array("h", pcm[: len(pcm) - (len(pcm) % 2)])
    if not samples:
        return 0.0
    rms = math.sqrt(sum(s * s for s in samples) / len(samples))
    return min(1.0, math.sqrt(rms / 9000.0))


def envelope(wav_bytes: bytes, step: float = 0.04) -> list:
    """Quanto è aperta la bocca ogni 40 ms (0..1), dall'audio vero della risposta."""
    try:
        with wave.open(io.BytesIO(wav_bytes)) as w:
            rate, width, ch = w.getframerate(), w.getsampwidth(), w.getnchannels()
            raw = w.readframes(w.getnframes())
    except (wave.Error, EOFError):
        return []
    if width != 2 or not raw:
        return []
    samples = array.array("h", raw[: len(raw) - (len(raw) % 2)])
    if ch > 1:
        samples = samples[::ch]
    n = max(1, int(rate * step))
    vals = []
    for i in range(0, len(samples), n):
        part = samples[i:i + n]
        vals.append(math.sqrt(sum(s * s for s in part) / len(part)) if part else 0.0)
    peak = max(vals) or 1.0
    return [round(min(1.0, v / peak) ** 0.7, 3) for v in vals]


# ---------------------------------------------------------------------------
# Testo da dire: niente emoji, simboli letti bene
# ---------------------------------------------------------------------------

_SAY = [(r"€", " euro"), (r"%", " per cento"), (r"°C?", " gradi"), (r"\s*[•·]\s*", ". "), (r"\s*→\s*", ", "), (r"\s*[–—]\s*", ", "),
        (r"(\d{1,2})/(\d{1,2})/(\d{4})", r"\1 \2 \3")]


def tts_text(text: str, limit: int = 600) -> str:
    """Il testo per la voce: via emoji e simboli, «€» diventa «euro», le righe diventano pause. Al massimo ~600 caratteri."""
    t = text or ""
    for pat, rep in _SAY:
        t = re.sub(pat, rep, t)
    drop_cats, drop_chars = ("So", "Sk", "Cs", "Co", "Cn", "Cf"), "\ufe0f\ufe0e\u20e3\u200d"   # emoji, simboli, selettori di variante
    t = "".join(ch for ch in t if ch not in drop_chars and unicodedata.category(ch) not in drop_cats)
    t = re.sub(r"\s*\n+\s*", ". ", t)
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"(\s*[.,;:]\s*){2,}", lambda m: m.group(0).strip()[0] + " ", t)
    t = t.strip(" .,;:") + ("." if t.strip(" .,;:") else "")
    if len(t) > limit:
        cut = max(t.rfind(". ", 0, limit), t.rfind("? ", 0, limit), t.rfind("! ", 0, limit))
        t = t[: cut + 1] if cut > limit // 3 else t[:limit].rsplit(" ", 1)[0] + "…"
    return t if re.search(r"\w", t) else ""


# ---------------------------------------------------------------------------
# Registrare
# ---------------------------------------------------------------------------

class Recorder:
    """Premi: start(). Lasci: stop() restituisce il WAV (o None se era troppo corto)."""

    def __init__(self, on_level=None, cmd=None, max_seconds: float = MAX_SECONDS):
        self.cmd = cmd or find_tool(RECORDERS, "EDITH_RECORDER_CMD")
        self.on_level, self.max_bytes = on_level, int(max_seconds * RATE * 2)
        self.proc, self.thread, self.buf = None, None, bytearray()
        self.limit_hit = False

    @property
    def recording(self) -> bool:
        return self.proc is not None

    def start(self):
        if not self.cmd:
            raise VoiceError("Non trovo un programma per registrare: serve parecord (pacchetto libpulse) o arecord (alsa-utils).")
        self.buf, self.limit_hit = bytearray(), False
        try:
            self.proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        except OSError as e:
            self.proc = None
            raise VoiceError(f"Non riesco ad avviare la registrazione ({self.cmd[0]}).") from e
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        proc = self.proc
        while proc and proc.stdout:
            chunk = proc.stdout.read(CHUNK)
            if not chunk:
                break
            self.buf += chunk
            if self.on_level:
                self.on_level(rms_level(chunk))
            if len(self.buf) >= self.max_bytes:
                self.limit_hit = True
                break

    def stop(self, keep: bool = True):
        proc, self.proc = self.proc, None
        if proc is None:
            return None
        try:
            proc.terminate()
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            proc.kill()
        if self.thread:
            self.thread.join(timeout=1.0)
        if self.on_level:
            self.on_level(0.0)
        pcm = bytes(self.buf[: self.max_bytes])
        if not keep or len(pcm) < MIN_SECONDS * RATE * 2:
            return None
        return build_wav(pcm)


# ---------------------------------------------------------------------------
# Parlare
# ---------------------------------------------------------------------------

class Speaker:
    """Piper in un processo che resta acceso (voce già caricata) e un lettore audio di sistema."""

    def __init__(self, python: str, model: str, speed: float = 1.0, worker: str = None, player=None):
        self.python, self.model, self.speed = python, model, speed
        self.worker_path = worker or str(HERE / "edith_tts_worker.py")
        self.player = player or find_tool(PLAYERS, "EDITH_PLAYER_CMD")
        self.proc, self.lines, self.lock = None, queue.Queue(), threading.Lock()
        self.playing = None
        self.tmp = Path(tempfile.gettempdir()) / f"edith-voice-{os.getuid()}"

    def problem(self):
        """None se si può parlare, altrimenti il motivo (da mostrare)."""
        if not Path(self.python).exists():
            return "Piper non è installato (manca il suo ambiente Python): guarda la guida dell'isola."
        if not Path(self.model).exists():
            return f"Manca la voce di Piper ({Path(self.model).name}): guarda la guida dell'isola."
        if not self.player:
            return "Non trovo un programma per suonare l'audio (pw-play, paplay o aplay)."
        return None

    def _start_worker(self):
        if self.proc and self.proc.poll() is None:
            return
        self.lines = queue.Queue()
        try:
            self.proc = subprocess.Popen([self.python, self.worker_path, self.model, str(self.speed)], stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        except OSError as e:
            self.proc = None
            raise VoiceError("Non riesco ad avviare Piper.") from e
        out, q = self.proc.stdout, self.lines

        def pump():
            for line in out:
                q.put(line.strip())
            q.put(None)
        threading.Thread(target=pump, daemon=True).start()
        first = self._line(40)                          # la prima volta carica la voce: qualche secondo
        if first != "ready":
            self.close()
            raise VoiceError("Piper non è partito: " + (first or "nessuna risposta").removeprefix("err ")[:120])

    def _line(self, timeout: float):
        try:
            return self.lines.get(timeout=timeout)
        except queue.Empty:
            return None

    def synthesize(self, text: str) -> bytes:
        text = tts_text(text)
        if not text:
            raise VoiceError("Niente da dire.")
        with self.lock:
            self._start_worker()
            self.tmp.mkdir(mode=0o700, exist_ok=True)
            out = self.tmp / f"say-{time.time_ns()}.wav"
            try:
                self.proc.stdin.write(json.dumps({"text": text, "out": str(out)}) + "\n")
                self.proc.stdin.flush()
            except (OSError, ValueError) as e:
                self.close()
                raise VoiceError("Piper si è fermato.") from e
            ans = self._line(30)
            if ans != "ok":
                if ans is None:
                    self.close()
                raise VoiceError("Piper non ha risposto." if ans is None else ans.removeprefix("err ")[:160])
            try:
                return out.read_bytes()
            finally:
                out.unlink(missing_ok=True)

    def play(self, wav_bytes: bytes) -> float:
        """Avvia la riproduzione (senza aspettare) e restituisce la durata in secondi."""
        self.stop()
        if not self.player:
            raise VoiceError("Non trovo un programma per suonare l'audio (pw-play, paplay o aplay).")
        self.tmp.mkdir(mode=0o700, exist_ok=True)
        f = self.tmp / "now.wav"
        f.write_bytes(wav_bytes)
        try:
            self.playing = subprocess.Popen(self.player + [str(f)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            raise VoiceError(f"Non riesco a suonare l'audio ({self.player[0]}).") from e
        return wav_seconds(wav_bytes) or 0.0

    def stop(self):
        p, self.playing = self.playing, None
        if p and p.poll() is None:
            p.terminate()

    def close(self):
        self.stop()
        p, self.proc = self.proc, None
        if p and p.poll() is None:
            try:
                p.stdin.close()
                p.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                p.kill()
