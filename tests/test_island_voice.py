"""La voce dell'isola: registrare tenendo premuto, rispondere a voce con Piper, la bocca a tempo con l'audio."""
import io
import math
import struct
import sys
import time
import wave
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ISLAND = ROOT / "desktop" / "island"
sys.path.insert(0, str(ISLAND))
import edith_core as C  # noqa: E402
import edith_voice as V  # noqa: E402

FAKE_PIPER = '''
import math, struct
class SynthesisConfig:
    def __init__(self, length_scale=None, **kw):
        self.length_scale = length_scale
class PiperVoice:
    @classmethod
    def load(cls, model_path, **kw):
        if "rotta" in str(model_path):
            raise RuntimeError("voce rovinata")
        return cls()
    def synthesize_wav(self, text, wav_file, syn_config=None, **kw):
        if "boom" in text:
            raise ValueError("testo che fa esplodere la voce finta")
        scale = syn_config.length_scale if syn_config and syn_config.length_scale else 1.0
        n = int(22050 * min(3.0, 0.05 * len(text) * scale))
        wav_file.setnchannels(1); wav_file.setsampwidth(2); wav_file.setframerate(22050)
        wav_file.writeframes(b"".join(struct.pack("<h", int(12000 * abs(math.sin(i / 2205)) * math.sin(i / 20))) for i in range(n)))
'''
FAKE_REC = '''
import math, signal, struct, sys, time
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
i = 0
while True:
    sys.stdout.buffer.write(b"".join(struct.pack("<h", int(9000 * math.sin(2 * math.pi * 220 * (i + k) / 16000))) for k in range(1600)))
    sys.stdout.buffer.flush(); i += 1600; time.sleep(0.1)
'''


def tone(seconds, amp=8000, rate=16000):
    return b"".join(struct.pack("<h", int(amp * math.sin(2 * math.pi * 220 * i / rate))) for i in range(int(rate * seconds)))


@pytest.fixture
def fake_piper(tmp_path, monkeypatch):
    pkg = tmp_path / "pp" / "piper"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(FAKE_PIPER)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "pp"))
    model = tmp_path / "it_IT-prova.onnx"
    model.write_bytes(b"finta")
    return model


@pytest.fixture
def fake_rec(tmp_path):
    p = tmp_path / "rec.py"
    p.write_text(FAKE_REC)
    return [sys.executable, str(p)]


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------

def test_wav_is_what_the_server_route_wants():
    w = V.build_wav(tone(1.5) + b"\x01")                                           # un byte in più non rompe nulla
    with wave.open(io.BytesIO(w)) as f:
        assert (f.getnchannels(), f.getsampwidth(), f.getframerate()) == (1, 2, 16000)
    assert V.wav_seconds(w) == pytest.approx(1.5, abs=0.001) and V.wav_seconds(b"non un wav") is None


def test_level_and_envelope():
    assert V.rms_level(b"\x00\x00" * 1600) == 0.0 and V.rms_level(struct.pack("<h", 32767) * 1600) == 1.0
    assert 0.3 < V.rms_level(tone(0.1, 3000)) < 0.8 and V.rms_level(b"") == 0.0
    w = V.build_wav(tone(0.4) + b"\x00\x00" * 6400 + tone(0.4))                   # parla, pausa, parla
    env = V.envelope(w)
    assert len(env) == pytest.approx(1.2 / 0.04, abs=1) and max(env) == 1.0
    assert all(v < 0.05 for v in env[11:19]) and min(env[:9]) > 0.8                # durante la pausa la bocca è chiusa
    assert V.envelope(b"rotto") == [] and V.envelope(V.build_wav(b"")) == []


@pytest.mark.parametrize("src,expected", [
    ("📅 Oggi alle 15:00 • Dentista 📍 Via Roma 12", "Oggi alle 15:00. Dentista Via Roma 12."),
    ("Hai speso 412,30 € (62%) questo mese", "Hai speso 412,30 euro (62 per cento) questo mese."),
    ("☑️ Promemoria\n• Chiamare il commercialista\n• Pile", "Promemoria. Chiamare il commercialista. Pile."),
    ("Domani 19/10/2026 ci sono 12°C", "Domani 19 10 2026 ci sono 12 gradi."),
    ("Ok!", "Ok!."),
    ("🎉🎉 ✅", ""),
    ("", ""),
])
def test_tts_text(src, expected):
    assert V.tts_text(src) == expected


def test_tts_text_cuts_long_answers_at_a_sentence():
    long = " ".join(f"Frase numero {i} di una risposta lunga." for i in range(60))
    out = V.tts_text(long, limit=200)
    assert len(out) <= 200 and out.endswith(".") and out.startswith("Frase numero 0")


def test_find_tool(monkeypatch):
    monkeypatch.delenv("EDITH_RECORDER_CMD", raising=False)
    assert V.find_tool(V.RECORDERS, "EDITH_RECORDER_CMD", which=lambda n: n == "arecord")[0] == "arecord"
    assert V.find_tool(V.RECORDERS, "EDITH_RECORDER_CMD", which=lambda n: True)[0] == "parecord"
    assert V.find_tool(V.RECORDERS, "EDITH_RECORDER_CMD", which=lambda n: False) is None
    monkeypatch.setenv("EDITH_RECORDER_CMD", "pw-record --rate 16000 -")
    assert V.find_tool(V.RECORDERS, "EDITH_RECORDER_CMD") == ["pw-record", "--rate", "16000", "-"]


# ---------------------------------------------------------------------------
# Registrare
# ---------------------------------------------------------------------------

def test_recorder_records_while_held_and_reports_the_level(fake_rec):
    levels = []
    r = V.Recorder(on_level=levels.append, cmd=fake_rec)
    r.start()
    assert r.recording
    time.sleep(1.0)
    w = r.stop()
    assert not r.recording and 0.6 <= V.wav_seconds(w) <= 1.4
    assert max(levels) > 0.3 and levels[-1] == 0.0                                 # alla fine il livello torna a zero


def test_recorder_ignores_taps_cancels_and_stops_at_the_limit(fake_rec):
    r = V.Recorder(cmd=fake_rec)
    r.start()
    time.sleep(0.05)
    assert r.stop() is None                                                        # un tocco: troppo corto, niente invio
    r.start()
    time.sleep(0.6)
    assert r.stop(keep=False) is None                                              # annullato (isola chiusa)
    r = V.Recorder(cmd=fake_rec, max_seconds=0.5)
    r.start()
    time.sleep(1.0)
    w = r.stop()
    assert r.limit_hit and V.wav_seconds(w) <= 0.5
    assert V.Recorder(cmd=fake_rec).stop() is None                                 # fermare senza aver iniziato non fa nulla


def test_recorder_without_any_program_says_what_to_install(monkeypatch):
    monkeypatch.delenv("EDITH_RECORDER_CMD", raising=False)
    monkeypatch.setattr(V.shutil, "which", lambda n: None)
    with pytest.raises(V.VoiceError) as e:
        V.Recorder().start()
    assert "parecord" in str(e.value) and "arecord" in str(e.value)
    with pytest.raises(V.VoiceError):
        V.Recorder(cmd=["/non/esiste/registratore"]).start()


# ---------------------------------------------------------------------------
# Parlare (con il vero programma che tiene Piper acceso, e un Piper finto con l'interfaccia della 1.8)
# ---------------------------------------------------------------------------

def test_speaker_keeps_the_voice_loaded_and_recovers(fake_piper):
    s = V.Speaker(sys.executable, str(fake_piper), 1.0, player=["true"])
    assert s.problem() is None
    w = s.synthesize("📅 Oggi hai tre impegni.")
    assert V.wav_seconds(w) > 0.3 and len(V.envelope(w)) > 5
    pid = s.proc.pid
    t = time.time()
    s.synthesize("Seconda frase, subito.")
    assert s.proc.pid == pid and time.time() - t < 3                              # stesso processo: la voce non si ricarica
    s.proc.kill()
    s.proc.wait()
    assert V.wav_seconds(s.synthesize("Dopo un crollo riparte.")) > 0              # si riavvia da solo
    with pytest.raises(V.VoiceError) as e:
        s.synthesize("boom")
    assert "esplodere" in str(e.value)
    assert V.wav_seconds(s.synthesize("E poi continua.")) > 0                      # un errore su una frase non lo ferma
    with pytest.raises(V.VoiceError):
        s.synthesize("🎉")                                                         # niente da dire
    assert s.play(w) == pytest.approx(V.wav_seconds(w))
    s.stop()
    s.close()
    assert s.proc is None


def test_speaker_speed_reaches_piper(fake_piper):
    slow, fast = V.Speaker(sys.executable, str(fake_piper), 2.0, player=["true"]), V.Speaker(sys.executable, str(fake_piper), 0.5, player=["true"])
    try:
        assert V.wav_seconds(slow.synthesize("Una frase di prova.")) > 3 * V.wav_seconds(fast.synthesize("Una frase di prova."))
    finally:
        slow.close()
        fast.close()


def test_speaker_explains_what_is_missing(fake_piper, tmp_path):
    assert "Piper non è installato" in V.Speaker("/non/esiste/python", str(fake_piper), player=["true"]).problem()
    assert "Manca la voce" in V.Speaker(sys.executable, str(tmp_path / "manca.onnx"), player=["true"]).problem()
    bad = tmp_path / "rotta.onnx"
    bad.write_bytes(b"x")
    s = V.Speaker(sys.executable, str(bad), player=["true"])
    with pytest.raises(V.VoiceError) as e:
        s.synthesize("ciao")
    assert "voce rovinata" in str(e.value)
    s.close()


# ---------------------------------------------------------------------------
# Rete, impostazioni, comandi
# ---------------------------------------------------------------------------

def test_voice_request_passes_the_real_route():
    """L'audio e le intestazioni che manda l'isola passano i controlli veri di /api/voice."""
    from test_web_app import LOGIN, make
    tc, _, asst = make()

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(req, timeout=None):
        headers = {k: v for k, v in req.header_items()}
        r = tc.post(req.full_url.replace("https://edith", ""), content=req.data, headers={**headers, **LOGIN})
        assert r.status_code == 200, r.text
        return Resp(r.content)
    c = C.Client("https://edith", "island-prova", opener)
    out = c.voice(V.build_wav(tone(1.2)))
    assert out["heard"] == "cosa faccio oggi" and out["speak"] == "Ho capito." and asst.voice[-1][0] == "island-prova"
    with pytest.raises(C.EdithError):
        c.voice(b"")


def test_voice_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(C, "CONFIG_DIR", tmp_path)
    for k in ("EDITH_ISLAND_VOICE", "EDITH_VOICE_SPEED", "EDITH_VOICE_MODEL", "EDITH_PIPER_PYTHON"):
        monkeypatch.delenv(k, raising=False)
    s = C.settings()
    assert s["EDITH_ISLAND_VOICE"] == "auto" and s["EDITH_VOICE_SPEED"] == "1.0"
    assert s["EDITH_VOICE_MODEL"].endswith("voices/it_IT-paola-medium.onnx") and s["EDITH_PIPER_PYTHON"].endswith("piper/bin/python")
    (tmp_path / "edith.conf").write_text("EDITH_ISLAND_VOICE=sempre\nEDITH_VOICE_SPEED=1,3\nEDITH_VOICE_MODEL=~/voci/x.onnx\n")
    s = C.settings()
    assert s["EDITH_ISLAND_VOICE"] == "auto" and s["EDITH_VOICE_SPEED"] == "1.3" and s["EDITH_VOICE_MODEL"] == str(Path.home() / "voci" / "x.onnx")
    monkeypatch.setenv("EDITH_VOICE_SPEED", "9")
    monkeypatch.setenv("EDITH_ISLAND_VOICE", "always")
    assert C.settings()["EDITH_VOICE_SPEED"] == "2.0" and C.settings()["EDITH_ISLAND_VOICE"] == "always"
    monkeypatch.setenv("EDITH_VOICE_SPEED", "piano")
    assert C.settings()["EDITH_VOICE_SPEED"] == "1.0"


def test_push_to_talk_commands(monkeypatch):
    from test_island import load_cli
    cli = load_cli()
    assert {"listen", "stop", "talk"} <= set(C.COMMANDS)
    sent, spawned = [], []
    monkeypatch.setattr(cli, "spawn", spawned.append)
    monkeypatch.setattr(cli.core, "send_command", lambda cmd, *a, **k: (sent.append(cmd), True)[1])
    for cmd in ("listen", "stop", "talk"):
        assert cli.main(["edith-island", cmd]) == 0
    assert sent == ["listen", "stop", "talk"] and spawned == []
    monkeypatch.setattr(cli.core, "send_command", lambda cmd, *a, **k: False)
    for cmd, start in (("listen", "listen"), ("talk", "listen"), ("stop", None)):
        spawned.clear()
        assert cli.main(["edith-island", cmd]) == 0 and spawned == ([start] if start else [])


def test_ui_keeps_audio_off_the_graphics_thread():
    src = (ISLAND / "edith_island_ui.py").read_text(encoding="utf-8")
    assert "in_thread(lambda: self.speaker.synthesize(say_text), done)" in src
    assert "in_thread(lambda: rec.stop(keep=send), done)" in src and "in_thread(lambda: self.client.voice(wav)" in src
    assert "GLib.idle_add(self.avatar.set_level, v)" in src                         # il livello arriva alla grafica nel modo giusto
    assert "self.stop_listening(send=False)" in src and "self.stop_voice()" in src   # chiudendo l'isola si smette di ascoltare e di parlare
