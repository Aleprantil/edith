"""Motore dell'assistente: ogni intent, conferme, memoria, errori e HTML sicuro. Servizi esterni sostituiti da finti."""
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from services import assistant as A
from services import monitor_service, news_service
from services import nextcloud_service as NC
from services import notes_service as NS

TZ = ZoneInfo("Europe/Rome")
NOW = datetime(2026, 10, 5, 10, 0, tzinfo=TZ)
SID = "sessione-di-prova"


def parsed(**kw):
    base = {"intent": "unknown", "title": "", "start_time": None, "end_time": None, "location": "", "category": "generale", "rrule": None,
            "description": "", "original_time": None, "amount": None, "finance_category": None, "period": None, "news_topic": None}
    base.update(kw)
    return base


@pytest.fixture
def eng(monkeypatch):
    script, contexts, texts = {}, [], []

    def fake_parse(text, now_str, context=""):
        contexts.append(context)
        texts.append(text)
        return script.get(text) or parsed()
    monkeypatch.setattr(A, "parse_user_input", fake_parse)
    e = A.Assistant(owner_id=42, clock=lambda: NOW)
    e.script, e.contexts, e.texts = script, contexts, texts
    return e


def ev(title, h, m=0, dur=60, cal="svago", loc="", day=0, recurring=False):
    s = NOW.replace(hour=h, minute=m) + timedelta(days=day)
    return {"title": title, "start": s, "end": s + timedelta(minutes=dur), "calendar": cal, "location": loc, "recurring": recurring}


# ---------------------------------------------------------------------------
# HTML sicuro
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("dirty,must_not", [
    ("<script>alert(1)</script>ciao", "<script"),
    ("<img src=x onerror=alert(1)>", "<img"),
    ('<a href="javascript:alert(1)">clic</a>', "javascript:"),
    ('<b onclick="x()">grassetto</b>', "onclick"),
    ("<iframe src='//evil'></iframe>", "<iframe"),
    ("<style>body{display:none}</style>x", "<style"),
    ("<svg onload=alert(1)>", "<svg"),
    ('<a href="data:text/html;base64,AAAA">x</a>', "data:"),
    ("<math><mi>x</mi></math>", "<math"),
])
def test_sanitizer_removes_dangerous_html(dirty, must_not):
    assert must_not not in A.sanitize_html(dirty).lower()


def test_sanitizer_keeps_the_formatting_we_use():
    out = A.sanitize_html('Ciao <b>mondo</b> <i>a</i> <code>x</code><br>fine <a href="https://ex.it/a?b=1&c=2">link</a>')
    assert "<b>mondo</b>" in out and "<i>a</i>" in out and "<code>x</code>" in out and "<br>" in out
    assert 'href="https://ex.it/a?b=1&amp;c=2"' in out and 'rel="noopener noreferrer"' in out and 'target="_blank"' in out


def test_sanitizer_closes_broken_tags_and_escapes_text():
    assert A.sanitize_html("<b><i>x</b>") == "<b><i>x</i></b>"
    assert A.sanitize_html("a < b & c > d") == "a &lt; b &amp; c &gt; d"
    assert A.sanitize_html("</b></i>solo testo") == "solo testo"
    assert A.sanitize_html("") == "" and A.sanitize_html(None) == ""


def test_markdown_lite_conversion_escapes_first():
    assert A.md_to_html("*grasso* _corsivo_ `cod`") == "<b>grasso</b> <i>corsivo</i> <code>cod</code>"
    assert "<script>" not in A.md_to_html("*<script>alert(1)</script>*")
    assert A.md_to_html("cura_persona e snake_case_name") == "cura_persona e snake_case_name"      # niente corsivi finti


def test_plain_text_for_speech_drops_tags_links_and_emoji():
    t = A.to_plain('✅ Evento <b>salvato</b>:<br>📅 05/10 <a href="https://x.it">sito</a> https://x.it/zz')
    assert "<" not in t and "http" not in t and "✅" not in t and "📅" not in t and "salvato" in t
    assert len(A.to_plain("parola " * 300)) <= 450


# ---------------------------------------------------------------------------
# Calendario
# ---------------------------------------------------------------------------

def test_create_event_happy_path_and_conflict_warning(eng, monkeypatch):
    calls = []
    monkeypatch.setattr(NC, "create_calendar_event", lambda **k: (calls.append(k), {"success": True, "calendar_used": "svago", "conflict": True})[1])
    eng.script["segna colazione"] = parsed(intent="create_event", title="Colazione", start_time="2026-10-06T08:00:00", location="Bar Centrale", category="svago")
    r = eng.handle(SID, "segna colazione")
    assert r.ok and "Colazione" in r.html and "06/10/2026 08:00" in r.html and "Bar Centrale" in r.html and "già un altro impegno" in r.html
    assert r.refresh == ["today"] and r.confirm is None
    assert calls[0]["title"] == "Colazione" and calls[0]["start_time_str"] == "2026-10-06T08:00:00"


def test_create_event_without_date_is_an_error_not_a_crash(eng, monkeypatch):
    monkeypatch.setattr(NC, "create_calendar_event", lambda **k: pytest.fail("non deve creare nulla"))
    eng.script["segna"] = parsed(intent="create_event", title="X")
    r = eng.handle(SID, "segna")
    assert not r.ok and "giorno e ora" in r.html


def test_create_event_nextcloud_failure_is_reported(eng, monkeypatch):
    monkeypatch.setattr(NC, "create_calendar_event", lambda **k: {"success": False, "message": "Nextcloud giù"})
    eng.script["segna"] = parsed(intent="create_event", title="X", start_time="2026-10-06T08:00:00")
    r = eng.handle(SID, "segna")
    assert not r.ok and "Nextcloud giù" in r.html


def test_event_title_with_html_is_escaped(eng, monkeypatch):
    monkeypatch.setattr(NC, "create_calendar_event", lambda **k: {"success": True, "calendar_used": "<b>x</b>", "conflict": False})
    eng.script["segna"] = parsed(intent="create_event", title="<script>alert(1)</script>", start_time="2026-10-06T08:00:00")
    html = eng.handle(SID, "segna").html
    assert "<script" not in html and "&lt;script&gt;" in html


def test_get_events_lists_and_filters(eng, monkeypatch):
    events = [ev("Dentista", 15, loc="Via Roma", cal="svago"), ev("Calcio", 18, cal="calcio"), {**ev("Compleanno", 0, cal="svago"), "start": NOW.date(), "end": NOW.date()}]
    monkeypatch.setattr(NC, "get_calendar_events", lambda a, b: events)
    eng.script["oggi"] = parsed(intent="get_events", start_time="2026-10-05T00:00:00")
    html = eng.handle(SID, "oggi").html
    assert "Dentista" in html and "Calcio" in html and "Via Roma" in html and "tutto il giorno" in html
    eng.script["calcio"] = parsed(intent="get_events", title="calcio", category="calcio")
    html = eng.handle(SID, "calcio").html
    assert "Calcio" in html and "Dentista" not in html
    eng.script["niente"] = parsed(intent="get_events", title="inesistente")
    assert "Non ho trovato" in eng.handle(SID, "niente").html


def test_update_event_asks_confirmation_and_changes_nothing_until_yes(eng, monkeypatch):
    found, moved = (ev("Dentista", 15), object()), []
    monkeypatch.setattr(NC, "find_event", lambda *a, **k: found)
    monkeypatch.setattr(NC, "update_found", lambda e, d, s, n=None: moved.append((e, s)))
    eng.script["sposta"] = parsed(intent="update_event", title="Dentista", original_time="2026-10-05T00:00:00", start_time="2026-10-06T10:00:00")
    r = eng.handle(SID, "sposta")
    assert r.confirm and "Dentista" in r.html and "06/10/2026 10:00" in r.html and moved == []
    done = eng.confirm(SID, r.confirm["token"], True)
    assert done.ok and "spostato" in done.html and done.refresh == ["today"] and len(moved) == 1 and moved[0][1].hour == 10


def test_delete_event_confirmation_yes_no_expired_and_single_use(eng, monkeypatch):
    deleted = []
    monkeypatch.setattr(NC, "find_event", lambda *a, **k: (ev("Allenamento", 18, cal="calcio"), "OGGETTO"))
    monkeypatch.setattr(NC, "delete_found", lambda e, d: deleted.append(e))
    eng.script["cancella"] = parsed(intent="delete_event", title="Allenamento")

    r = eng.handle(SID, "cancella")
    assert r.confirm and "Cancello" in r.html and "Allenamento" in r.html and deleted == []
    no = eng.confirm(SID, r.confirm["token"], False)
    assert "non faccio nulla" in no.html and deleted == []
    assert not eng.confirm(SID, r.confirm["token"], True).ok and deleted == []             # già usato: non si cancella lo stesso

    r = eng.handle(SID, "cancella")
    assert eng.confirm(SID, r.confirm["token"], True).ok and deleted == ["OGGETTO"]
    assert not eng.confirm(SID, r.confirm["token"], True).ok and deleted == ["OGGETTO"]     # un token vale una sola volta

    r = eng.handle(SID, "cancella")
    eng._session(SID).pending[r.confirm["token"]] = (lambda: pytest.fail("scaduto"), time.time() - 1)
    expired = eng.confirm(SID, r.confirm["token"], True)
    assert not expired.ok and "scaduta" in expired.html


def test_confirmation_tokens_belong_to_their_session(eng, monkeypatch):
    deleted = []
    monkeypatch.setattr(NC, "find_event", lambda *a, **k: (ev("X", 9), "OGG"))
    monkeypatch.setattr(NC, "delete_found", lambda e, d: deleted.append(e))
    eng.script["cancella"] = parsed(intent="delete_event", title="X")
    token = eng.handle("sessione-uno-aaa", "cancella").confirm["token"]
    assert not eng.confirm("sessione-due-bbb", token, True).ok and deleted == []


def test_only_nine_pending_confirmations_are_kept(eng, monkeypatch):
    monkeypatch.setattr(NC, "find_event", lambda *a, **k: (ev("X", 9), "OGG"))
    eng.script["cancella"] = parsed(intent="delete_event", title="X")
    tokens = [eng.handle(SID, "cancella").confirm["token"] for _ in range(12)]
    assert len(eng._session(SID).pending) == 9 and tokens[0] not in eng._session(SID).pending and tokens[-1] in eng._session(SID).pending


def test_event_not_found_and_missing_title_and_recurring_refusal(eng, monkeypatch):
    monkeypatch.setattr(NC, "find_event", lambda *a, **k: None)
    eng.script["cancella"] = parsed(intent="delete_event", title="Fantasma")
    assert "Non ho trovato" in eng.handle(SID, "cancella").html
    eng.script["cancella2"] = parsed(intent="delete_event")
    assert "quale evento" in eng.handle(SID, "cancella2").html
    eng.script["sposta"] = parsed(intent="update_event", title="X")
    assert "Non ho capito" in eng.handle(SID, "sposta").html

    def recurring(*a, **k):
        raise NC.CalendarError("'calcio' è un evento ricorrente: non lo cancello")
    monkeypatch.setattr(NC, "find_event", recurring)
    eng.script["cancella3"] = parsed(intent="delete_event", title="calcio")
    r = eng.handle(SID, "cancella3")
    assert not r.ok and r.confirm is None and "ricorrente" in r.html


def test_free_slots_for_a_range_and_for_a_day(eng, monkeypatch):
    monkeypatch.setattr(NC, "get_calendar_events", lambda a, b: [ev("Lezione", 17, dur=60)])
    eng.script["libero 17"] = parsed(intent="find_free_slots", start_time="2026-10-05T17:00:00", end_time="2026-10-05T18:00:00")
    assert "hai già" in eng.handle(SID, "libero 17").html
    eng.script["libero 19"] = parsed(intent="find_free_slots", start_time="2026-10-05T19:00:00", end_time="2026-10-05T20:00:00")
    assert "Sì, sei libero" in eng.handle(SID, "libero 19").html
    monkeypatch.setattr(NC, "get_free_slots", lambda d, c: [(NOW.replace(hour=12), NOW.replace(hour=15))])
    eng.script["libero"] = parsed(intent="find_free_slots", start_time="2026-10-05T00:00:00")
    assert "dalle 12:00 alle 15:00" in eng.handle(SID, "libero").html
    monkeypatch.setattr(NC, "get_free_slots", lambda d, c: [])
    assert "Non hai tempo libero" in eng.handle(SID, "libero").html


# ---------------------------------------------------------------------------
# Note, finanze, news, server
# ---------------------------------------------------------------------------

def test_notes_add_list_and_read(eng, monkeypatch):
    monkeypatch.setattr(NS, "save_note_to_nextcloud", lambda t, c, cat="": {"success": True, "title": t, "folder": "Note"})
    eng.script["nota"] = parsed(intent="add_note", title="Spesa", description="latte, <b>uova</b>")
    r = eng.handle(SID, "nota")
    assert "Nota salvata" in r.html and "latte" in r.html and r.refresh == ["notes"] and "<b>uova</b>" not in r.html
    monkeypatch.setattr(NS, "list_notes", lambda cat="": [{"title": "Spesa"}, {"title": "Idee"}])
    eng.script["note"] = parsed(intent="get_notes")
    assert "Spesa" in eng.handle(SID, "note").html
    monkeypatch.setattr(NS, "get_note_content", lambda t, c="": "# Spesa\n<script>x</script>latte")
    eng.script["leggi"] = parsed(intent="get_notes", title="Spesa")
    html = eng.handle(SID, "leggi").html
    assert "latte" in html and "<script" not in html
    monkeypatch.setattr(NS, "get_note_content", lambda t, c="": None)
    assert not eng.handle(SID, "leggi").ok


class FakeFinance:
    async def add_expense(self, uid, amount, cat, note, ts):
        self.last = ("exp", uid, amount, cat, note)
        return "✅ Spesa registrata!\n*🍕 Ristoranti* — €12,00\n📝 _pizza_"

    async def add_income(self, uid, amount, cat, note, ts):
        return "✅ Entrata registrata!\n*💼 Stipendio* — €1.500,00 🎉"

    async def get_summary(self, uid, period, cat, a, b):
        self.last = ("sum", period)
        return "📊 *Riepilogo ottobre 2026*\n💚 Entrate: €0,00"


def test_finance_intents_use_owner_id_and_convert_markdown(eng, monkeypatch):
    fin = FakeFinance()
    monkeypatch.setattr(A, "_finance", lambda: fin)
    eng.script["spesa"] = parsed(intent="add_expense", amount=12, finance_category="ristoranti", title="pizza")
    r = eng.handle(SID, "spesa")
    assert "<b>🍕 Ristoranti</b>" in r.html and "<i>pizza</i>" in r.html and r.refresh == ["finance"] and fin.last == ("exp", 42, 12, "ristoranti", "pizza")
    eng.script["entrata"] = parsed(intent="add_income", amount=1500)
    assert "Stipendio" in eng.handle(SID, "entrata").html
    eng.script["riepilogo"] = parsed(intent="get_finance_summary", period="ieri")
    assert "Riepilogo ottobre" in eng.handle(SID, "riepilogo").html and fin.last == ("sum", "ieri")


def test_finance_not_installed_is_a_clear_message(eng, monkeypatch):
    def missing():
        raise ImportError("no finance")
    monkeypatch.setattr(A, "_finance", missing)
    eng.script["spesa"] = parsed(intent="add_expense", amount=12)
    r = eng.handle(SID, "spesa")
    assert not r.ok and "non sono configurate" in r.html


def test_finance_coroutines_run_from_a_worker_thread(eng, monkeypatch):
    import threading
    out = {}
    monkeypatch.setattr(A, "_finance", lambda: FakeFinance())
    eng.script["spesa"] = parsed(intent="add_expense", amount=1)
    t = threading.Thread(target=lambda: out.setdefault("r", eng.handle(SID, "spesa")))
    t.start(); t.join(5)
    assert out["r"].ok


def test_news_and_server_status(eng, monkeypatch):
    g = [{"folder": "sport", "feed": "Gazzetta", "all_ids": [], "items": [{"id": "1", "title": "Inter-Milan", "line": "Inter-Milan 2-1", "url": "https://x.it/1", "source": "G", "pub": 0}]}]
    monkeypatch.setattr(news_service, "collect_latest", lambda topic: (g, ["sport"]))
    eng.script["news"] = parsed(intent="get_news", news_topic="calcio")
    html = eng.handle(SID, "news").html
    assert "Inter-Milan 2-1" in html and 'href="https://x.it/1"' in html and "Ultime notizie" in html
    monkeypatch.setattr(news_service, "collect_latest", lambda topic: ([], ["sport"]))
    assert "Non ho trovato notizie" in eng.handle(SID, "news").html

    class Mon:
        def server_report(self):
            return "🖥️ <b>Server</b> · test\n🧠 <b>CPU</b> 5%"
    monkeypatch.setattr(monitor_service, "MONITOR", Mon())
    eng.script["server"] = parsed(intent="get_server_status")
    r = eng.handle(SID, "server")
    assert "CPU" in r.html and r.refresh == ["server"]


# ---------------------------------------------------------------------------
# Memoria, errori, limiti
# ---------------------------------------------------------------------------

def test_recent_conversation_is_given_to_gemini_but_not_across_sessions(eng, monkeypatch):
    monkeypatch.setattr(NC, "create_calendar_event", lambda **k: {"success": True, "calendar_used": "svago", "conflict": False})
    eng.script["segna colazione"] = parsed(intent="create_event", title="Colazione", start_time="2026-10-06T08:00:00")
    eng.handle("sessione-uno-aaa", "segna colazione")
    eng.handle("sessione-uno-aaa", "spostalo alle 10")
    assert eng.contexts[0] == "" and "Utente: segna colazione" in eng.contexts[1] and "Assistente:" in eng.contexts[1] and "Colazione" in eng.contexts[1]
    eng.handle("sessione-due-bbb", "ciao")
    assert eng.contexts[2] == ""


def test_history_is_limited(eng):
    for i in range(10):
        eng.handle(SID, f"messaggio {i}")
    ctx = eng.contexts[-1]                    # contesto della decima richiesta: gli ultimi 4 messaggi PRECEDENTI (5-8... esclusa quella in corso)
    assert ctx.count("Utente:") == A.HISTORY_TURNS and "messaggio 8" in ctx and "messaggio 9" not in ctx and "messaggio 4" not in ctx


def test_gemini_failure_and_unknown_intent_and_empty_text(eng, monkeypatch):
    monkeypatch.setattr(A, "parse_user_input", lambda t, n, c="": {"intent": "unknown", "error": True})
    r = eng.handle(SID, "ciao")
    assert not r.ok and "Gemini" in r.html
    monkeypatch.setattr(A, "parse_user_input", lambda t, n, c="": parsed())
    r = eng.handle(SID, "blabla")
    assert r.ok and "Non ho capito" in r.html
    assert not eng.handle(SID, "   ").ok


def test_unexpected_exception_is_hidden_from_user(eng, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("password=segretissima")
    monkeypatch.setattr(NC, "get_calendar_events", boom)
    eng.script["oggi"] = parsed(intent="get_events")
    r = eng.handle(SID, "oggi")
    assert not r.ok and "segretissima" not in r.html and "RuntimeError" not in r.html


def test_calendar_errors_are_shown_with_their_cause(eng, monkeypatch):
    def down(*a, **k):
        raise NC.CalendarError("Nextcloud reindirizza a /login")
    monkeypatch.setattr(NC, "get_calendar_events", down)
    eng.script["oggi"] = parsed(intent="get_events")
    r = eng.handle(SID, "oggi")
    assert not r.ok and "reindirizza" in r.html


def test_text_is_truncated_and_sessions_are_bounded(eng):
    eng.handle(SID, "x" * 5000)
    assert len(eng.texts[-1]) == A.MAX_TEXT                # a Gemini non arrivano mai più di 500 caratteri
    for i in range(A.MAX_SESSIONS + 10):
        eng.handle(f"sessione-numero-{i:03d}", "ciao")
    assert len(eng.sessions) <= A.MAX_SESSIONS


def test_reply_to_dict_shape(eng):
    d = eng.handle(SID, "blabla").to_dict()
    assert set(d) == {"html", "ok", "refresh", "confirm", "speak", "heard"} and d["speak"] and d["heard"] == ""
