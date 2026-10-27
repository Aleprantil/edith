"""I moduli di EDITH: base sempre presente, moduli scelti nel .env (EDITH_PLUGINS)."""
import pytest

from services import plugins as P


@pytest.fixture(autouse=True)
def restore():
    yield
    P.set_enabled(None)


def test_without_the_setting_everything_is_on(monkeypatch):
    monkeypatch.delenv("EDITH_PLUGINS", raising=False)
    assert P.enabled(refresh=True) == list(P.CATALOG)
    for v in ("all", "tutti", "*", "  "):
        monkeypatch.setenv("EDITH_PLUGINS", v)
        assert P.enabled(refresh=True) == list(P.CATALOG)


def test_none_means_only_the_base(monkeypatch):
    for v in ("none", "NESSUNO", "base"):
        monkeypatch.setenv("EDITH_PLUGINS", v)
        assert P.enabled(refresh=True) == []


def test_parsing_keeps_catalog_order_ignores_unknown_and_duplicates(monkeypatch, caplog):
    monkeypatch.setenv("EDITH_PLUGINS", " Server, calendar,boh,calendar, ,tasks")
    assert P.enabled(refresh=True) == ["calendar", "tasks", "server"]
    assert "boh" in caplog.text


def test_a_module_whose_requirement_is_off_is_switched_off(monkeypatch, caplog):
    fake = P.Plugin("extra", "Extra", "prova", requires=("notes",))
    monkeypatch.setitem(P.CATALOG, "extra", fake)
    P.set_enabled(["extra", "calendar"])
    assert P.enabled() == ["calendar"] and "richiede notes" in caplog.text
    P.set_enabled(["extra", "notes"])
    assert P.enabled() == ["notes", "extra"]


def test_every_intent_belongs_to_exactly_one_module_and_matches_the_engine():
    from services import assistant as A
    owners = {}
    for p in P.CATALOG.values():
        for i in p.intents:
            assert i not in owners, i
            owners[i] = p.name
    src = open(A.__file__, encoding="utf-8").read()
    handled = set(__import__("re").findall(r'"([a-z_]+)": self\._[a-z_]+', src)) - {"token"}
    assert handled == set(owners), handled ^ set(owners)                               # niente richieste senza modulo, niente moduli senza codice


def test_intents_cards_and_examples_follow_the_modules():
    P.set_enabled(["calendar", "notes"])
    assert P.intent_on("create_event") and not P.intent_on("add_expense") and P.intent_on("unknown")
    assert P.card_on("today") and P.card_on("notes") and not P.card_on("finance") and not P.card_on("server")
    assert P.examples() == ["«cosa faccio oggi?»", "«prendi nota: comprare il latte»"]
    P.set_enabled([])
    assert P.active_intents() == [] and P.card_on("today") and P.examples() == ["«ciao»"]


def test_gemini_only_learns_about_active_modules():
    from services import gemini_service as G
    full = G.build_prompt("ciao", "2026-10-21 10:00:00")
    for i in [i for p in P.CATALOG.values() for i in p.intents]:
        assert f'"{i}"' in full, i
    P.set_enabled(["tasks"])
    only = G.build_prompt("ciao", "2026-10-21 10:00:00")
    assert '"add_task"' in only and '"add_expense"' not in only and '"create_event"' not in only and '"get_news"' not in only
    assert "finance_category" not in only and "news_topic" not in only and "rrule" not in only and '"category"' not in only
    assert len(only) < len(full) / 2


def test_prompt_uses_settings_not_personal_data(monkeypatch):
    from services import gemini_service as G
    monkeypatch.setattr(G, "CALENDAR_CATEGORIES", ["casa", "palestra", "generale"])
    monkeypatch.setattr(G, "ASSISTANT_NAME", "JARVIS")
    p = G.build_prompt("ciao", "2026-10-21 10:00:00")
    assert 'ti chiami JARVIS' in p and '["casa", "palestra", "generale"]' in p
    assert "EDITH" not in p                                                             # il nome viene dalle impostazioni
    assert p.count('"category"') == 1 and '"allenamento"' not in p                      # i calendari pure: niente di scritto nel codice
    P.set_enabled(["notes"])
    assert "la cartella della nota" in G.build_prompt("x", "2026-10-21 10:00:00")


def test_engine_refuses_a_request_of_a_switched_off_module(monkeypatch):
    from services import assistant as A
    from test_assistant import parsed
    monkeypatch.setattr(A, "parse_user_input", lambda text, now_str, context="", **kw: parsed(intent="add_expense", amount=10))
    P.set_enabled(["calendar"])
    r = A.Assistant(owner_id=42).handle("prova", "ho speso 10 euro")
    assert "Finanze" in r.html and "non è attivo" in r.html
    monkeypatch.setattr(A, "parse_user_input", lambda text, now_str, context="", **kw: parsed(intent="unknown"))
    r = A.Assistant(owner_id=42).handle("prova", "boh")
    assert "«cosa faccio oggi?»" in r.html and "pizza" not in r.html                   # esempi solo dei moduli attivi


def test_memory_is_skipped_when_its_module_is_off(monkeypatch):
    from services import assistant as A
    from test_assistant import parsed
    seen = {}

    def fake_parse(text, now_str, context="", **kw):
        seen.update(kw)
        return parsed(intent="unknown")
    monkeypatch.setattr(A, "parse_user_input", fake_parse)
    monkeypatch.setattr(A.memory_service, "prompt_block", lambda: "- il calcio è il martedì")
    P.set_enabled(["calendar"])
    A.Assistant(owner_id=42).handle("prova", "ricordati che il calcio è il martedì")
    assert "memory" not in seen                                                       # né comandi di memoria, né memoria a Gemini
    P.set_enabled(["calendar", "memory"])
    A.Assistant(owner_id=42).handle("prova", "cosa faccio?")
    assert seen.get("memory") == "- il calcio è il martedì"


def test_cards_of_switched_off_modules_are_reported_unavailable(monkeypatch):
    from web import cards
    P.set_enabled(["calendar"])
    with pytest.raises(cards.CardUnavailable):
        cards.get_card("finance")
    assert cards.active_order() == ["today"]
    P.set_enabled(None)
    assert cards.active_order() == cards.ORDER


def test_today_card_without_calendar_and_weather(monkeypatch):
    from web import cards
    called = []
    monkeypatch.setattr(cards.nextcloud_service, "get_calendar_events", lambda a, b: called.append("cal") or [])
    monkeypatch.setattr(cards.weather_service, "get_forecast", lambda force=False: called.append("meteo") or [])
    P.set_enabled([])
    d = cards.card_today()
    assert called == [] and d["events"] == [] and d["weather"] is None and d["date_label"]
    P.set_enabled(["calendar", "weather"])
    cards.card_today()
    assert called == ["cal", "meteo"]


# ---------------------------------------------------------------------------
# Frasi per passione
# ---------------------------------------------------------------------------

def test_every_pack_is_known_and_has_phrases():
    import json
    from pathlib import Path
    doc = json.loads((Path(__file__).resolve().parent.parent / "web" / "static" / "frasi.json").read_text(encoding="utf-8"))
    known = set(doc["pacchetti"])
    used = [p for f in doc["frasi"] for p in f.get("p", [])]
    assert set(used) <= known and all(used.count(k) >= 5 for k in known)
    assert sum(1 for f in doc["frasi"] if not f.get("p")) >= 30                         # le frasi per tutti restano tante


def test_the_server_sends_only_the_chosen_packs():
    from test_web_app import make
    c, st, _ = make()
    everything = c.get("/static/frasi.json").json()["frasi"]
    st.quote_packs = ["sci"]
    only = c.get("/static/frasi.json").json()["frasi"]
    assert len(only) < len(everything)
    assert all(not f.get("p") or "sci" in f["p"] for f in only)
    assert any("sci" in f.get("p", []) for f in only) and not any(f.get("p") == ["calcio"] for f in only)


def test_the_page_receives_only_active_cards():
    from test_web_app import LOGIN, make
    c, _, _ = make()
    P.set_enabled(["calendar", "notes"])
    assert c.get("/api/me", headers=LOGIN).json()["cards"] == ["today", "notes"]
