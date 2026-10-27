"""Voce: rotte audio e loro difese, frasi parlate (a voce, non lette), conferma a voce, trascrizione."""
import struct
import time
from datetime import timedelta

import pytest

from services import assistant as A
from services import gemini_service as G
from services import monitor_service, news_service
from services import nextcloud_service as NC
from test_assistant import NOW, SID, ev, parsed          # stessi dati finti del motore
from test_web_app import API, LOGIN, FakeAssistant, make

AUDIO = {"X-Requested-With": "edith", "Content-Type": "audio/wav", "X-Session": "session-abcdef123", **LOGIN}


def wav(seconds=1.0, rate=16000, channels=1, bits=16, tail=b""):
    n = int(rate * seconds) * 2
    data = b"\x01\x02" * (n // 2)
    hdr = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, channels, rate, rate * channels * bits // 8, channels * bits // 8, bits)
    return hdr + b"data" + struct.pack("<I", len(data)) + data + tail


# ---------------------------------------------------------------------------
# Rotte audio
# ---------------------------------------------------------------------------

def test_voice_happy_path_and_transcribe():
    c, _, asst = make()
    r = c.post("/api/voice", content=wav(), headers=AUDIO)
    assert r.status_code == 200 and r.json()["heard"] == "cosa faccio oggi" and r.json()["speak"] == "Ho capito." and asst.voice == [("session-abcdef123", 32044)]
    r = c.post("/api/transcribe", content=wav(), headers=AUDIO)
    assert r.status_code == 200 and r.json() == {"text": "sì"}


def test_voice_requires_login_header_audio_type_and_same_site():
    c, _, asst = make()
    no_login = {k: v for k, v in AUDIO.items() if k != "Tailscale-User-Login"}
    assert c.post("/api/voice", content=wav(), headers=no_login).status_code == 401
    assert c.post("/api/voice", content=wav(), headers={k: v for k, v in AUDIO.items() if k != "X-Requested-With"}).status_code == 400
    assert c.post("/api/voice", content=wav(), headers={**AUDIO, "Content-Type": "text/plain"}).status_code == 400
    assert c.post("/api/voice", content=wav(), headers={**AUDIO, "Content-Type": "application/json"}).status_code == 400
    assert c.post("/api/voice", content=wav(), headers={**AUDIO, "Sec-Fetch-Site": "cross-site"}).status_code == 400
    assert c.post("/api/voice", content=wav(), headers={**AUDIO, "X-Session": "../x"}).status_code == 400
    assert c.post("/api/transcribe", content=wav(), headers=no_login).status_code == 401
    assert asst.voice == []


@pytest.mark.parametrize("body", [b"", b"non sono un wav", wav(rate=44100), wav(channels=2), wav(bits=8), wav(seconds=0.05), wav(seconds=29),
                                  wav()[:30], wav()[:-100], b"RIFF" + b"\x00" * 60])
def test_voice_rejects_bad_audio(body):
    c, _, asst = make()
    assert c.post("/api/voice", content=body, headers=AUDIO).status_code == 400
    assert c.post("/api/transcribe", content=body, headers=AUDIO).status_code == 400
    assert asst.voice == []


def test_voice_rejects_oversized_upload_before_reading():
    c, *_ = make()
    assert c.post("/api/voice", content=wav(seconds=31), headers=AUDIO).status_code == 400


def test_voice_is_rate_limited_together_with_text():
    c, _, asst = make()
    codes = [c.post("/api/voice", content=wav(), headers=AUDIO).status_code for _ in range(32)]
    assert codes[:30] == [200] * 30 and set(codes[30:]) == {429} and len(asst.voice) == 30


def test_slow_voice_gets_a_clear_timeout(monkeypatch):
    import web.app as webapp
    monkeypatch.setattr(webapp, "ASSISTANT_TIMEOUT", 0.2)
    c, _, asst = make()
    asst.handle_voice = lambda sid, w: (time.sleep(1.0), None)[1]
    r = c.post("/api/voice", content=wav(), headers=AUDIO)
    assert r.status_code == 200 and r.json()["ok"] is False and "troppo" in r.json()["speak"]


def test_transcribe_failure_is_reported_without_details():
    c, _, asst = make()
    asst.transcribe = lambda w: (_ for _ in ()).throw(RuntimeError("chiave=segreta"))
    r = c.post("/api/transcribe", content=wav(), headers=AUDIO)
    assert r.status_code == 200 and r.json()["text"] == "" and "segreta" not in r.text


def test_spoken_flag_reaches_the_assistant_and_me_has_a_nickname():
    c, st, asst = make()
    from web.app import create_app
    from starlette.testclient import TestClient
    h = {**LOGIN, **API}
    c.post("/api/assistant", json={"text": "ciao", "session": SID, "spoken": True}, headers=h)
    c.post("/api/assistant", json={"text": "ciao", "session": SID, "spoken": "si"}, headers=h)          # solo il booleano vero conta
    c.post("/api/assistant", json={"text": "ciao", "session": SID}, headers=h)
    c.post("/api/confirm", json={"session": SID, "token": "t", "yes": True, "spoken": True}, headers=h)
    assert asst.spoken == [True, False, False, True]
    assert c.get("/api/me", headers=LOGIN).json()["nick"] == "Tester"                                   # senza soprannome: il nome
    st.owner_nick = "Mario"
    assert TestClient(create_app(st, asst)).get("/api/me", headers=LOGIN).json()["nick"] == "Mario"


# ---------------------------------------------------------------------------
# Come si dice a voce
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("h,m,expected", [(9, 0, "alle 9"), (9, 30, "alle 9 e mezza"), (15, 15, "alle 15 e un quarto"), (8, 5, "alle 8 e 5"),
                                          (1, 0, "all'una"), (0, 0, "a mezzanotte"), (12, 0, "a mezzogiorno"), (18, 45, "alle 18 e 45")])
def test_times_are_said_the_italian_way(h, m, expected):
    assert A.say_time(NOW.replace(hour=h, minute=m)) == expected


@pytest.mark.parametrize("days,expected", [(0, "oggi"), (1, "domani"), (2, "dopodomani"), (-1, "ieri"), (3, "giovedì"), (6, "domenica"), (9, "il 14 ottobre")])
def test_days_are_said_relative_to_today(days, expected):
    assert A.say_day((NOW + timedelta(days=days)).date(), NOW) == expected          # NOW è lunedì 5 ottobre


@pytest.mark.parametrize("x,expected", [(12, "12 euro"), (12.5, "12 euro e 50"), (0.99, "0 euro e 99"), ("7,5", "l'importo"), (None, "l'importo"), (-3, "3 euro")])
def test_amounts_are_said_in_euros(x, expected):
    assert A.say_amount(x) == expected


# ---------------------------------------------------------------------------
# Risposte parlate
# ---------------------------------------------------------------------------

@pytest.fixture
def eng(monkeypatch):
    script, rewrites = {}, []
    monkeypatch.setattr(A, "parse_user_input", lambda text, now_str, context="": script.get(text) or parsed())
    monkeypatch.setattr(A, "speech_rewrite", lambda plain, intent="": (rewrites.append((plain, intent)), "Riassunto parlato.")[1])
    e = A.Assistant(owner_id=42, clock=lambda: NOW)
    e.script, e.rewrites = script, rewrites
    return e


def test_events_are_summarised_not_read_out(eng, monkeypatch):
    monkeypatch.setattr(NC, "get_calendar_events", lambda a, b: [ev("Lezione di Software Engineering", 9, loc="Aula A104"), ev("Call con il gruppo", 11, 30),
                                                                 ev("Dentista", 15), ev("Allenamento", 18, 30), ev("Cena", 21)])
    eng.script["oggi"] = parsed(intent="get_events", start_time="2026-10-05T00:00:00")
    r = eng.handle(SID, "oggi", spoken=True)
    assert r.speak == ("Oggi hai 5 impegni. Alle 9, Lezione di Software Engineering; poi alle 11 e mezza, Call con il gruppo; poi alle 15, Dentista. E altri 2.")
    assert "Aula A104" not in r.speak and "<" not in r.speak and eng.rewrites == []             # niente chiamata in più a Gemini
    assert "Aula A104" in r.html                                                               # lo schermo resta completo
    assert eng.handle(SID, "oggi", spoken=False).speak != r.speak and "Aula" in eng.handle(SID, "oggi").speak        # senza «spoken» come prima


def test_events_on_several_days_name_the_day(eng, monkeypatch):
    monkeypatch.setattr(NC, "get_calendar_events", lambda a, b: [ev("Colazione", 8, day=1), ev("Esame", 14, day=3)])
    eng.script["settimana"] = parsed(intent="get_events", start_time="2026-10-05T00:00:00", end_time="2026-10-12T00:00:00")
    assert eng.handle(SID, "settimana", spoken=True).speak == "Hai 2 impegni. Domani alle 8, Colazione; poi giovedì alle 14, Esame."


def test_single_event_and_no_events(eng, monkeypatch):
    monkeypatch.setattr(NC, "get_calendar_events", lambda a, b: [ev("Dentista", 15)])
    eng.script["oggi"] = parsed(intent="get_events", start_time="2026-10-05T00:00:00")
    assert eng.handle(SID, "oggi", spoken=True).speak == "Oggi hai un impegno. Alle 15, Dentista."
    eng.script["niente"] = parsed(intent="get_events", title="inesistente")
    assert eng.handle(SID, "niente", spoken=True).speak == "Non ho trovato impegni."


def test_actions_confirm_briefly(eng, monkeypatch):
    monkeypatch.setattr(NC, "create_calendar_event", lambda **k: {"success": True, "calendar_used": "svago", "conflict": True})
    eng.script["segna"] = parsed(intent="create_event", title="Colazione", start_time="2026-10-06T08:00:00")
    assert eng.handle(SID, "segna", spoken=True).speak == "Fatto. Ho messo Colazione domani alle 8. Attenzione, hai già un altro impegno a quell'ora."

    monkeypatch.setattr(A, "_finance", lambda: type("F", (), {"add_expense": staticmethod(lambda *a: _coro("✅ Spesa registrata!\n*🍕 Ristoranti* — €12,00"))})())
    eng.script["spesa"] = parsed(intent="add_expense", amount=12.5, finance_category="ristoranti", title="pizza")
    assert eng.handle(SID, "spesa", spoken=True).speak == "Fatto, ho registrato 12 euro e 50 per pizza."
    monkeypatch.setattr(A, "_finance", lambda: type("F", (), {"add_expense": staticmethod(lambda *a: _coro("❌ Non ho capito l'importo."))})())
    r = eng.handle(SID, "spesa", spoken=True)
    assert "Fatto" not in r.speak and "importo" in r.speak and r.refresh == []                 # se non è registrata non dice «fatto»


async def _coro(value):
    return value


def test_move_and_delete_ask_for_a_spoken_yes_or_no(eng, monkeypatch):
    found, moved = (ev("Dentista", 15), object()), []
    monkeypatch.setattr(NC, "find_event", lambda *a, **k: found)
    monkeypatch.setattr(NC, "update_found", lambda e, d, st, n=None: moved.append(st))
    monkeypatch.setattr(NC, "delete_found", lambda e, d: moved.append("cancellato"))
    eng.script["sposta"] = parsed(intent="update_event", title="Dentista", original_time="2026-10-05T00:00:00", start_time="2026-10-06T10:00:00")
    r = eng.handle(SID, "sposta", spoken=True)
    assert r.speak == "Vuoi che sposti Dentista domani alle 10? Dimmi sì o no." and r.confirm and moved == []
    done = eng.confirm(SID, r.confirm["token"], True, spoken=True)
    assert done.speak == "Fatto, ho spostato Dentista domani alle 10." and len(moved) == 1

    eng.script["cancella"] = parsed(intent="delete_event", title="Dentista")
    r = eng.handle(SID, "cancella", spoken=True)
    assert r.speak == "Vuoi che cancelli Dentista di oggi alle 15? Dimmi sì o no."
    assert eng.confirm(SID, r.confirm["token"], True, spoken=True).speak == "Fatto, ho cancellato Dentista."
    r = eng.handle(SID, "cancella", spoken=True)
    assert eng.confirm(SID, r.confirm["token"], False, spoken=True).speak == "Va bene, non faccio nulla." and moved[-1] == "cancellato"
    r = eng.handle(SID, "cancella")                                                              # a testo: la domanda resta quella di prima
    assert "Dimmi sì o no" not in r.speak and r.confirm


def test_long_answers_are_rewritten_by_gemini_only_when_spoken(eng, monkeypatch):
    g = [{"folder": "sport", "feed": "G", "all_ids": [], "items": [{"id": "1", "title": "Inter", "line": "Inter-Milan 2-1", "url": "https://x.it/1", "source": "G", "pub": 0}]}]
    monkeypatch.setattr(news_service, "collect_latest", lambda topic: (g, ["sport"]))
    eng.script["news"] = parsed(intent="get_news")
    assert eng.handle(SID, "news", spoken=True).speak == "Riassunto parlato." and eng.rewrites[0][1] == "get_news" and "Inter-Milan" in eng.rewrites[0][0]
    eng.rewrites.clear()
    assert "Riassunto" not in eng.handle(SID, "news").speak and eng.rewrites == []              # a testo non si spende la chiamata

    class Mon:
        def server_report(self):
            return "🖥️ <b>Server</b>\n🧠 <b>CPU</b> 5%"
    monkeypatch.setattr(monitor_service, "MONITOR", Mon())
    eng.script["server"] = parsed(intent="get_server_status")
    assert eng.handle(SID, "server", spoken=True).speak == "Riassunto parlato." and eng.rewrites[-1][1] == "get_server_status"


def test_if_the_rewrite_fails_it_falls_back_to_plain_text(eng, monkeypatch):
    monkeypatch.setattr(A, "speech_rewrite", lambda plain, intent="": "")
    monkeypatch.setattr(A.notes_service, "list_notes", lambda cat="": [{"title": "Spesa"}, {"title": "Idee"}])
    eng.script["note"] = parsed(intent="get_notes")
    r = eng.handle(SID, "note", spoken=True)
    assert "Spesa" in r.speak and "<" not in r.speak and len(r.speak) <= 221


def test_errors_are_spoken_plainly(eng):
    eng.script["segna"] = parsed(intent="create_event", title="X")
    r = eng.handle(SID, "segna", spoken=True)
    assert not r.ok and "giorno e ora" in r.speak and eng.rewrites == []


# ---------------------------------------------------------------------------
# Ordini parlati
# ---------------------------------------------------------------------------

def test_voice_order_goes_through_the_same_pipeline_and_returns_what_was_heard(eng, monkeypatch):
    monkeypatch.setattr(A, "transcribe_audio", lambda w: "cosa faccio oggi")
    monkeypatch.setattr(NC, "get_calendar_events", lambda a, b: [ev("Dentista", 15)])
    eng.script["cosa faccio oggi"] = parsed(intent="get_events", start_time="2026-10-05T00:00:00")
    r = eng.handle_voice(SID, b"wav")
    assert r.heard == "cosa faccio oggi" and r.speak == "Oggi hai un impegno. Alle 15, Dentista." and r.to_dict()["heard"] == "cosa faccio oggi"


def test_voice_unintelligible_or_failed_transcription(eng, monkeypatch):
    monkeypatch.setattr(A, "transcribe_audio", lambda w: "")
    r = eng.handle_voice(SID, b"wav")
    assert not r.ok and r.speak == "Non ho sentito bene, puoi ripetere?" and r.heard == ""

    def boom(w):
        raise G.AudioError("Non riesco a trascrivere la voce adesso. Riprova, o scrivimi.")
    monkeypatch.setattr(A, "transcribe_audio", boom)
    r = eng.handle_voice(SID, b"wav")
    assert not r.ok and "Non riesco a trascrivere" in r.speak


# ---------------------------------------------------------------------------
# Gemini: trascrizione e riscrittura (con un modello finto)
# ---------------------------------------------------------------------------

class FakeResp:
    def __init__(self, text):
        self.text = text


def test_transcribe_audio_sends_wav_and_cleans_the_answer(monkeypatch):
    seen = {}

    def fake(parts, **kw):
        seen["parts"], seen["kw"] = parts, kw
        return FakeResp('  "Cosa faccio oggi?"  ')
    monkeypatch.setattr(G._audio_model, "generate_content", fake)
    assert G.transcribe_audio(b"RIFFxx") == "Cosa faccio oggi?"
    assert seen["parts"][1] == {"mime_type": "audio/wav", "data": b"RIFFxx"} and "italiano" in seen["parts"][0] and seen["kw"]["generation_config"]["temperature"] == 0


@pytest.mark.parametrize("answer", ["[inaudibile]", "[Inaudibile].", "", "  "])
def test_transcribe_audio_unintelligible_is_empty(monkeypatch, answer):
    monkeypatch.setattr(G._audio_model, "generate_content", lambda parts, **kw: FakeResp(answer))
    assert G.transcribe_audio(b"x") == ""


def test_transcribe_audio_failure_raises_a_friendly_error(monkeypatch):
    def boom(parts, **kw):
        raise RuntimeError("400 modello senza audio, chiave=SEGRETA")
    monkeypatch.setattr(G._audio_model, "generate_content", boom)
    with pytest.raises(G.AudioError) as e:
        G.transcribe_audio(b"x")
    assert "SEGRETA" not in str(e.value)


def test_speech_rewrite_prompt_and_failure(monkeypatch):
    seen = {}
    monkeypatch.setattr(G._model, "generate_content", lambda p, **kw: (seen.setdefault("p", p), FakeResp("Hai speso   340 euro.\n"))[1])
    assert G.speech_rewrite("Riepilogo ottobre ...", "get_finance_summary") == "Hai speso 340 euro."
    assert "A VOCE" in seen["p"] and "non inventare" in seen["p"] and "quanto è stato speso" in seen["p"] and "Riepilogo ottobre" in seen["p"]
    monkeypatch.setattr(G._model, "generate_content", lambda p, **kw: (_ for _ in ()).throw(RuntimeError("giù")))
    assert G.speech_rewrite("testo") == ""


def test_static_audio_supports_range_requests_which_iphone_needs(tmp_path):
    """Safari su iPhone riproduce un audio solo se il server risponde alle richieste a intervalli (206)."""
    from starlette.testclient import TestClient
    from web import security
    from web.app import create_app
    (tmp_path / "intro.mp3").write_bytes(bytes(range(256)) * 40)
    st = security.WebSettings(allowed_logins=["me@example.com"], secret=b"k")
    c = TestClient(create_app(st, FakeAssistant(), static_dir=str(tmp_path)))
    full = c.get("/static/intro.mp3")
    assert full.status_code == 200 and full.headers["content-type"].startswith("audio/") and full.headers.get("accept-ranges") == "bytes"
    part = c.get("/static/intro.mp3", headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and len(part.content) == 100 and part.headers["content-range"].startswith("bytes 0-99/")
    assert c.get("/static/intro.mp3", headers={"Range": "bytes=10000-20000"}).status_code == 206             # 10.240 byte: intervallo in parte valido
    assert c.get("/static/intro.mp3", headers={"Range": "bytes=20000-30000"}).status_code == 416              # del tutto fuori: errore previsto, non del server
