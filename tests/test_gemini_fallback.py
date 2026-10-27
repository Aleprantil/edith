"""Il modello di riserva di Gemini: se il principale è sovraccarico, la stessa richiesta passa all'altro."""
import importlib
import time

import pytest
from google.api_core import exceptions as gexc

from services import gemini_service as G


class R:
    def __init__(self, text):
        self.text = text


@pytest.fixture
def setup(monkeypatch):
    calls = []

    class M:
        def __init__(self, name, behaviour):
            self.name, self.behaviour = name, behaviour

        def generate_content(self, contents, **kw):
            calls.append((self.name, kw["request_options"]["timeout"]))
            out = self.behaviour(contents) if callable(self.behaviour) else self.behaviour
            if isinstance(out, Exception):
                raise out
            return R(out)

    def make(primary, fallback, kind="text"):
        monkeypatch.setattr(G, "_model" if kind == "text" else "_audio_model", M("principale", primary))
        monkeypatch.setattr(G, "_fallback_model" if kind == "text" else "_audio_fallback_model", None if fallback is None else M("riserva", fallback))
        G._prefer_fallback_until.update(text=0.0, audio=0.0)
        calls.clear()
        return calls
    yield make
    G._prefer_fallback_until.update(text=0.0, audio=0.0)


@pytest.mark.parametrize("err", [gexc.DeadlineExceeded("504 Deadline expired"), gexc.ServiceUnavailable("503"), gexc.ResourceExhausted("429 quota"),
                                 gexc.InternalServerError("500"), gexc.NotFound("404 models/x is not found"), TimeoutError(),
                                 RuntimeError("This model is currently experiencing high demand")])
def test_overload_goes_to_the_fallback(setup, err):
    calls = setup(err, "dalla riserva")
    assert G._generate("text", "ciao", 10, 20).text == "dalla riserva"
    assert calls == [("principale", 10), ("riserva", 20)]                          # il principale ha poco tempo, la riserva di più


def test_after_a_failure_the_fallback_goes_first_then_the_primary_comes_back(setup, monkeypatch):
    calls = setup(gexc.ServiceUnavailable("503"), "ok")
    G._generate("text", "a", 10, 20)
    calls.clear()
    G._generate("text", "b", 10, 20)
    assert calls == [("riserva", 20)]                                              # per 10 minuti non si aspetta il principale
    monkeypatch.setattr(G._fallback_model, "behaviour", gexc.ServiceUnavailable("503"))
    monkeypatch.setattr(G._model, "behaviour", "di nuovo il principale")
    calls.clear()
    assert G._generate("text", "c", 10, 20).text == "di nuovo il principale" and calls == [("riserva", 20), ("principale", 10)]
    assert G._prefer_fallback_until["text"] == 0.0                                 # il principale è tornato: si riparte da lui
    G._prefer_fallback_until["text"] = time.time() - 1                             # finestra scaduta
    calls.clear()
    G._generate("text", "d", 10, 20)
    assert calls[0][0] == "principale"


def test_real_errors_are_not_hidden(setup):
    calls = setup(ValueError("prompt sbagliato"), "riserva")
    with pytest.raises(ValueError):
        G._generate("text", "x", 10, 20)
    assert calls == [("principale", 10)]                                           # non è un sovraccarico: niente riserva
    calls = setup(gexc.ServiceUnavailable("503"), gexc.ServiceUnavailable("anche lei"))
    with pytest.raises(gexc.ServiceUnavailable) as e:
        G._generate("text", "x", 10, 20)
    assert "anche lei" in str(e.value) and calls == [("principale", 10), ("riserva", 20)]


def test_without_a_fallback_the_primary_gets_all_the_time(setup):
    calls = setup(gexc.ServiceUnavailable("503"), None)
    with pytest.raises(gexc.ServiceUnavailable):
        G._generate("text", "x", 10, 20)
    assert calls == [("principale", 20)]


def test_the_four_real_calls_use_the_fallback(setup):
    setup(gexc.DeadlineExceeded("504"), '{"intent": "get_events", "start_time": "2026-10-20T00:00:00"}')
    assert G.parse_user_input("cosa faccio oggi", "2026-10-20T10:00:00")["intent"] == "get_events"
    setup(gexc.ServiceUnavailable("503"), "Hai speso 340 euro.")
    assert G.speech_rewrite("Speso: 340 €", "get_finance_summary") == "Hai speso 340 euro."
    setup(gexc.ServiceUnavailable("503"), '{"a": "titolo"}')
    assert G.translate_headlines({"a": "headline"}) == {"a": "titolo"}
    calls = setup(gexc.DeadlineExceeded("504"), "cosa faccio oggi", kind="audio")
    assert G.transcribe_audio(b"RIFF....") == "cosa faccio oggi" and calls == [("principale", 12), ("riserva", 25)]
    assert G._prefer_fallback_until["audio"] > time.time() and G._prefer_fallback_until["text"] == 0.0   # voce e testo si ricordano a parte
    setup(ValueError("audio rotto"), "x", kind="audio")
    with pytest.raises(G.AudioError):
        G.transcribe_audio(b"RIFF....")


def test_worst_case_stays_within_the_page_timeout():
    from web.app import ASSISTANT_TIMEOUT
    assert 12 + 25 + 10 + 20 <= ASSISTANT_TIMEOUT                                   # voce: trascrizione + comprensione, tutte e due con riserva


def test_fallback_setting(monkeypatch):
    import config
    try:
        for value, expected in ((None, "gemini-flash-latest"), ("gemini-flash-latest", "gemini-flash-latest"), ("off", ""), (" none ", "")):
            if value is None:
                monkeypatch.delenv("GEMINI_FALLBACK_MODEL", raising=False)
            else:
                monkeypatch.setenv("GEMINI_FALLBACK_MODEL", value)
            assert importlib.reload(config).GEMINI_FALLBACK_MODEL == expected
    finally:
        monkeypatch.delenv("GEMINI_FALLBACK_MODEL", raising=False)
        importlib.reload(config)
