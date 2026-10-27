"""Il widget di EDITH per Waybar (extras/hyprland/edith-waybar.py)."""
import importlib.util
import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("edith_waybar", ROOT / "desktop" / "edith-waybar.py")
W = importlib.util.module_from_spec(spec)
spec.loader.exec_module(W)

TZ = ZoneInfo("Europe/Rome")
NOW = datetime(2026, 10, 6, 12, 50, tzinfo=TZ)                         # martedì 6 ottobre, 12:50


def ev(title, h, m=0, dur=60, day=0, **kw):
    s = datetime(2026, 10, 6 + day, h, m, tzinfo=TZ)
    return {"title": title, "calendar": kw.get("calendar", "università"), "location": kw.get("location", ""), "all_day": False,
            "time": f"{h:02d}:{m:02d}", "end": (s + timedelta(minutes=dur)).strftime("%H:%M"), "past": s + timedelta(minutes=dur) <= NOW,
            "start_iso": s.isoformat(), "end_iso": (s + timedelta(minutes=dur)).isoformat()}


TODAY = {"date_label": "martedì 6 ottobre", "events": [ev("Lezione", 9), ev("Robotics", 13, 30, 180, location="Aula A105"), ev("Allenamento bambini", 17, 50, 90, calendar="calcio")],
         "upcoming": [{"label": "Domani", "events": [ev("Laboratorio", 8, 30, 120, day=1)]}],
         "weather": {"emoji": "⛅", "tmin": 9, "tmax": 19, "label": "parzialmente nuvoloso"},
         "weather_days": [{"label": "oggi", "emoji": "⛅", "tmin": 9, "tmax": 19, "desc": "nuvoloso", "pop": 20},
                          {"label": "domani", "emoji": "🌧️", "tmin": 8, "tmax": 14, "desc": "pioggia", "pop": 70}],
         "sun": {"rise": "07:12", "set": "18:03"}}
TASKS = {"lists": [{"name": "TO DO", "items": [{"title": "Chiamare il commercialista", "label": "scaduto ieri", "level": "overdue"},
                                                 {"title": "Comprare le pile", "label": "oggi", "level": "today"}]}], "total": 2, "overdue": 1}
SERVER = {"host": "edith-server", "cpu": {"pct": 12.4}, "mem": {"pct": 37.0}, "temp": {"cpu": 54.0}, "root": {"pct": 61.0},
          "nextcloud": {"ok": True, "ms": 48}, "alerts": [], "mounts": [{"path": "/mnt/data", "mounted": True, "ro": False}]}


# ---------------------------------------------------------------------------
# Prossimo impegno
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mins,label", [(0, "ora"), (-3, "ora"), (1, "tra 1 min"), (59, "tra 59 min"), (60, "tra 1 h"), (95, "tra 1 h 35 min"), (300, "tra 5 h")])
def test_in_label(mins, label):
    assert W.in_label(mins) == label


def test_next_event_cases():
    assert W.next_event(TODAY, NOW)["when"] == "tra 41 min" and W.next_event(TODAY, NOW)["title"] == "Robotics"
    cur = W.next_event(TODAY, NOW.replace(hour=14))
    assert cur == {"when": "in corso", "title": "Robotics", "loc": "Aula A105", "extra": "fino alle 16:30", "now": True}
    assert W.next_event(TODAY, NOW.replace(hour=16, minute=40))["when"] == "tra 1 h 11 min"
    late = {**TODAY, "events": [ev("Cena", 21)]}
    assert W.next_event(late, NOW.replace(hour=9))["when"] == "alle 21:00"                      # oltre 6 ore: l'orario
    tomorrow = W.next_event(TODAY, NOW.replace(hour=20))
    assert tomorrow["when"] == "domani 08:30" and tomorrow["title"] == "Laboratorio"
    week = {"events": [], "upcoming": [{"label": "giovedì 8 ottobre", "events": [ev("Esame", 14, day=2)]}]}
    assert W.next_event(week, NOW)["when"] == "giovedì 14:00"
    assert W.next_event({"events": [{"title": "Compleanno", "all_day": True, "start_iso": None, "end_iso": None}], "upcoming": []}, NOW) is None
    assert W.next_event({}, NOW) is None and W.next_event(None, NOW) is None


# ---------------------------------------------------------------------------
# Barra e suggerimento
# ---------------------------------------------------------------------------

def test_bar_text_class_and_tooltip():
    out = W.compose({"today": TODAY, "tasks": TASKS, "server": SERVER}, NOW, "Nessun multiverso è stato danneggiato.", style="full")
    assert set(out) == {"text", "tooltip", "class"} and out["class"] == "overdue"
    assert "#9be15d" in out["text"] and "tra 41 min" in out["text"] and "Robotics" in out["text"] and "☑ 1" in out["text"]
    tip = out["tooltip"]
    for needle in ("martedì 6 ottobre", "🌧️ domani 8–14° (70%)", "🌅 07:12", "<b>Prossimo</b>", "📍 Aula A105", "<b>Oggi</b>", "Allenamento bambini",
                   "2 aperti, 1 scaduto", "Chiamare il commercialista", "#ff5d4d", "CPU 12% · RAM 37% · 54° · disco 61% · Nextcloud 48 ms", "› Nessun multiverso"):
        assert needle in tip, needle
    assert "Lezione" not in tip.split("<b>Oggi</b>")[1].split("<b>Promemoria")[0]                # gli impegni già finiti non si ripetono
    json.dumps(out)


@pytest.mark.parametrize("data,stale,cls", [
    ({"today": TODAY, "tasks": {"overdue": 0}, "server": SERVER}, None, "ok"),
    ({"today": TODAY, "tasks": {"overdue": 0}, "server": SERVER}, "now", "now"),
    ({"today": TODAY, "tasks": TASKS, "server": {**SERVER, "alerts": [{"html": "<b>Disco</b> quasi pieno"}]}}, None, "alert"),
    ({"today": TODAY, "tasks": TASKS, "server": {**SERVER, "nextcloud": {"ok": False}}}, None, "alert"),
    ({"today": TODAY, "tasks": TASKS, "server": {**SERVER, "mounts": [{"path": "/mnt/data", "mounted": False}]}}, None, "alert"),
    ({"today": TODAY, "tasks": TASKS, "server": {**SERVER, "alerts": [{"html": "x"}]}}, 1.0, "offline")])
def test_class_priority(data, stale, cls):
    now = NOW.replace(hour=14) if stale == "now" else NOW
    assert W.compose(data, now, None, None if stale == "now" else stale)["class"] == cls


def test_titles_with_special_characters_cannot_break_the_bar():
    evil = {**TODAY, "events": [ev("<b>Riunione</b> & co <span foreground='red'>", 13, 30)]}
    tasks = {"lists": [{"items": [{"title": "Pane & <latte>", "label": "<i>oggi</i>", "level": "today"}]}], "total": 1, "overdue": 0}
    out = W.compose({"today": evil, "tasks": tasks, "server": {**SERVER, "host": "a<b>&"}}, NOW, "Frase <tag> & co", style="full")
    assert "<b>Riunione</b>" not in out["text"] and "&lt;b&gt;Riunione" in out["text"] and "&amp;" in out["text"]
    for raw in ("<b>Riunione", "<latte>", "<i>oggi", "a<b>&", "<tag>"):
        assert raw not in out["tooltip"], raw
    # il markup che resta è solo il nostro
    assert set(re.findall(r"</?(\w+)", out["text"] + out["tooltip"])) <= {"span", "b", "i"}


def test_long_titles_are_shortened():
    out = W.compose({"today": {**TODAY, "events": [ev("Programmazione Avanzata - Laboratorio di gruppo con consegna", 13, 30)]}}, NOW, style="full")
    assert "Programmazione Avanzata…" in out["text"] and len(W.short("x" * 100)) == W.TITLE_MAX


def test_stale_data_say_so_and_failures_are_clear():
    out = W.compose({"today": TODAY}, NOW, None, stale_since=time.mktime(datetime(2026, 10, 6, 12, 10).timetuple()))
    assert out["class"] == "offline" and "non raggiungibile: dati delle 12:10" in out["tooltip"] and "#8d8071" in out["text"]
    assert W.failure("auth")["class"] == "auth" and "WEB_ALLOWED_LOGINS" in W.failure("auth")["tooltip"]
    assert W.failure("offline")["class"] == "offline" and "Tailscale" in W.failure("offline")["tooltip"]


# ---------------------------------------------------------------------------
# Frasi
# ---------------------------------------------------------------------------

POOL = json.loads((ROOT / "web" / "static" / "frasi.json").read_text(encoding="utf-8"))["frasi"]
APP_JS = (ROOT / "web" / "static" / "app.js").read_text(encoding="utf-8")
JS_FLAGS = set(re.findall(r"\bf\.(\w+)\s*=", APP_JS[APP_JS.index("function flags("):APP_JS.index("function pickQuote(")]))


def test_widget_knows_the_same_conditions_as_the_page():
    data = {"today": TODAY, "tasks": TASKS, "server": SERVER}
    assert set(W.flags(data, NOW)) == JS_FLAGS                                                   # se la pagina ne aggiunge una, questo test lo segnala
    f = W.flags(data, NOW)
    assert f["martedi"] and f["pomeriggio"] and not f["weekend"] and f["allenamento"] and f["lezione"] and f["scaduti"] and f["server_ok"] and not f["giornata_libera"]


def test_quote_is_stable_for_the_day_changes_with_offset_and_fits_the_moment():
    data = {"today": TODAY, "tasks": TASKS, "server": SERVER}
    a = W.quote_of_the_day(POOL, data, NOW)
    assert a and a == W.quote_of_the_day(POOL, data, NOW)                                         # stessa frase per la stessa giornata e lo stesso momento
    picks = {W.quote_of_the_day(POOL, data, NOW, o) for o in range(30)}
    assert len(picks) > 10                                                                        # --quote cambia davvero
    f = W.flags(data, NOW)
    for o in range(60):
        q = next(x for x in POOL if x["t"] == W.quote_of_the_day(POOL, data, NOW, o))
        assert all(f.get(k) for k in q.get("c", [])), q                                           # mai una frase fuori contesto
    assert W.quote_of_the_day([], data, NOW) is None


# ---------------------------------------------------------------------------
# Rete, cache, domande
# ---------------------------------------------------------------------------

def test_session_id_is_what_the_server_accepts(monkeypatch):
    for host in ("archlinux", "a", "PC di Mario.local", "x" * 90, "ä@!"):
        monkeypatch.setattr(W.socket, "gethostname", lambda h=host: h)
        assert re.fullmatch(r"[A-Za-z0-9_-]{8,64}", W.session_id())


def test_plain_text_for_notifications():
    assert W.plain("📅 <b>Ecco</b><br>• 09:00 Lezione &amp; lab") == "📅 Ecco\n• 09:00 Lezione & lab"


def test_one_failing_card_does_not_hide_the_others_and_auth_errors_surface(monkeypatch):
    def fake(url, body=None, timeout=4):
        if url.endswith("/tasks"):
            raise TimeoutError("lento")
        return {"ok": True, "data": {"name": url.rsplit("/", 1)[1]}}
    monkeypatch.setattr(W, "http_json", fake)
    out = W.fetch_cards("https://x")
    assert out["today"] == {"name": "today"} and out["server"] == {"name": "server"} and "tasks" not in out
    monkeypatch.setattr(W, "http_json", lambda *a, **k: (_ for _ in ()).throw(W.AuthError("HTTP 401")))
    with pytest.raises(W.AuthError):
        W.fetch_cards("https://x")
    monkeypatch.setattr(W, "http_json", lambda *a, **k: (_ for _ in ()).throw(OSError("giù")))
    with pytest.raises(ConnectionError):
        W.fetch_cards("https://x")


def test_bar_uses_recent_cache_when_offline_and_gives_up_when_too_old(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(W, "CACHE", tmp_path)
    monkeypatch.setattr(W, "http_json", lambda *a, **k: (_ for _ in ()).throw(OSError("giù")))
    W.save_cache("last.json", {"at": time.time() - 600, "data": {"today": TODAY, "tasks": TASKS, "server": SERVER}})
    W.show_bar({"EDITH_URL": "https://x", "EDITH_SIGNAL": "8"})
    out = json.loads(capsys.readouterr().out)
    assert out["class"] == "offline" and "non raggiungibile" in out["tooltip"] and "Robotics" in out["tooltip"]
    W.save_cache("last.json", {"at": time.time() - 7 * 3600, "data": {"today": TODAY}})
    W.show_bar({"EDITH_URL": "https://x", "EDITH_SIGNAL": "8"})
    assert json.loads(capsys.readouterr().out)["text"].endswith("EDITH offline")


def test_bar_online_saves_cache_and_shows_a_quote(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(W, "CACHE", tmp_path)

    def fake(url, body=None, timeout=4):
        if url.endswith("frasi.json"):
            return {"frasi": [{"t": "Una frase sempre valida."}]}
        name = url.rsplit("/", 1)[1]
        return {"ok": True, "data": {"today": TODAY, "tasks": TASKS, "server": SERVER}[name]}
    monkeypatch.setattr(W, "http_json", fake)
    W.show_bar({"EDITH_URL": "https://x", "EDITH_SIGNAL": "8"})
    out = json.loads(capsys.readouterr().out)
    assert "Una frase sempre valida." in out["tooltip"] and (tmp_path / "last.json").exists() and (tmp_path / "frasi.json").exists()
    monkeypatch.setattr(W, "http_json", lambda *a, **k: (_ for _ in ()).throw(W.AuthError("401")))
    W.show_bar({"EDITH_URL": "https://x", "EDITH_SIGNAL": "8"})
    assert json.loads(capsys.readouterr().out)["class"] == "auth"


def test_ask_posts_to_the_assistant_and_notifies(monkeypatch):
    seen, notes, refreshed = {}, [], []
    monkeypatch.setattr(W, "http_json", lambda url, body=None, timeout=4: (seen.update(url=url, body=body, timeout=timeout), {"html": "📅 <b>Oggi</b> hai 3 impegni", "ok": True, "confirm": None})[1])
    monkeypatch.setattr(W, "notify", lambda t, b: notes.append((t, b)))
    monkeypatch.setattr(W, "refresh", lambda conf: refreshed.append(1))
    W.ask({"EDITH_URL": "https://x", "EDITH_SIGNAL": "8"}, "cosa faccio oggi?")
    assert seen["url"] == "https://x/api/assistant" and seen["body"]["text"] == "cosa faccio oggi?" and re.fullmatch(r"[A-Za-z0-9_-]{8,64}", seen["body"]["session"])
    assert notes == [("EDITH", "📅 Oggi hai 3 impegni")] and refreshed == [1] and seen["timeout"] == 80
    monkeypatch.setattr(W, "http_json", lambda *a, **k: {"html": "Cancello <b>Dentista</b>?", "ok": True, "confirm": {"token": "t"}})
    W.ask({"EDITH_URL": "https://x", "EDITH_SIGNAL": "8"}, "cancella il dentista")
    assert "Serve una conferma" in notes[-1][1]
    W.ask({"EDITH_URL": "https://x", "EDITH_SIGNAL": "8"}, "   ")
    assert len(notes) == 2                                                                        # niente testo: niente richiesta


def test_the_real_server_accepts_what_the_widget_sends():
    """Il corpo che manda --ask passa davvero i controlli della rotta /api/assistant."""
    from test_web_app import LOGIN, make
    c, _, asst = make()
    r = c.post("/api/assistant", json={"text": "ciao", "session": W.session_id()}, headers={**LOGIN, "X-Requested-With": "edith"})
    assert r.status_code == 200 and asst.calls[-1][1] == "ciao"
    for name in ("today", "tasks", "server"):
        assert c.get(f"/api/cards/{name}", headers=LOGIN).status_code == 200



# ---------------------------------------------------------------------------
# Barra compatta (di serie)
# ---------------------------------------------------------------------------

def visible(markup: str) -> str:
    return re.sub(r"<[^>]+>", "", markup)


def test_compact_bar_is_short_and_the_details_stay_in_the_tooltip():
    data = {"today": TODAY, "tasks": TASKS, "server": SERVER}
    soon = W.compose(data, NOW)                                                  # Robotics tra 41 minuti
    assert visible(soon["text"]) == "◉  41 min  ☑ 1" and "Robotics" not in soon["text"] and "Robotics" in soon["tooltip"]
    assert visible(W.compose(data, NOW.replace(hour=14))["text"]) == "◉  in corso  ☑ 1"
    far = W.compose({**data, "tasks": {"overdue": 0}}, NOW.replace(hour=7, minute=0))        # la lezione delle 9 è tra 2 ore
    assert visible(far["text"]) == "◉" and "Lezione" in far["tooltip"]
    assert visible(W.compose({"today": {**TODAY, "events": []}, "tasks": {"overdue": 0}}, NOW)["text"]) == "◉"
    assert visible(W.compose(data, NOW, stale_since=1.0)["text"]).startswith("◌")
    for h in range(0, 24):
        assert len(visible(W.compose(data, NOW.replace(hour=h, minute=5))["text"])) <= 18      # mai più di una manciata di caratteri


def test_bar_style_can_be_switched_back_to_full(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(W, "CACHE", tmp_path)

    def fake(url, body=None, timeout=4):
        if url.endswith("frasi.json"):
            return {"frasi": []}
        return {"ok": True, "data": {"today": TODAY, "tasks": TASKS, "server": SERVER}[url.rsplit("/", 1)[1]]}
    monkeypatch.setattr(W, "http_json", fake)
    # show_bar usa l'ora vera: gli impegni di prova (ottobre 2026) sono passati, resta «domani 08:30 Laboratorio»
    W.show_bar({"EDITH_URL": "https://x", "EDITH_SIGNAL": "8", "EDITH_BAR_STYLE": "compact"})
    assert "Laboratorio" not in json.loads(capsys.readouterr().out)["text"]
    W.show_bar({"EDITH_URL": "https://x", "EDITH_SIGNAL": "8", "EDITH_BAR_STYLE": "full"})
    assert "Laboratorio" in json.loads(capsys.readouterr().out)["text"]
    assert W.settings()["EDITH_BAR_STYLE"] in ("compact", "full")


# ---------------------------------------------------------------------------
# Lo stile «come l'orologio»
# ---------------------------------------------------------------------------

spec_css = importlib.util.spec_from_file_location("waybar_css", ROOT / "desktop" / "waybar-stile-come-orologio.py")
CSS = importlib.util.module_from_spec(spec_css)
spec_css.loader.exec_module(CSS)
OLD_BLOCK = (ROOT / "desktop" / "waybar-stile.css").read_text(encoding="utf-8")

THEME = """* { font-family: JetBrainsMono; }
/* moduli */
#clock,
#battery, #cpu {
    background: #1e1e2e;
    border-radius: 12px;
    padding: 0 10px;
}
#clock:hover { background: #313244; }
.modules-center #clock { margin: 4px; }
#clockwork { color: red; }
@media (max-width: 800px) {
    #clock { padding: 0 4px; }
}
"""


def test_style_copies_every_clock_rule_and_removes_the_old_transparent_block():
    css, n = CSS.transform(THEME + "\n" + OLD_BLOCK)
    assert n == 4
    assert "#battery, #cpu,\n#custom-edith {" in css or "#cpu,\n#custom-edith {" in css
    assert "#clock:hover,\n#custom-edith:hover {" in css and ".modules-center #clock,\n.modules-center #custom-edith {" in css
    assert "#clockwork { color: red; }" in css and "#custom-editwork" not in css                 # #clockwork non è l'orologio
    assert "@media (max-width: 800px) {\n    #clock,\n#custom-edith {" in css
    assert CSS.OLD_MARKER not in css and "tooltip {" not in css and CSS.MARKER in css            # via lo sfondo trasparente e il tooltip
    assert "/* moduli */" in css and css.startswith("* { font-family")


def test_style_is_idempotent_and_refuses_unknown_themes(tmp_path, monkeypatch):
    monkeypatch.setattr(CSS.subprocess, "run", lambda *a, **k: None)
    f = tmp_path / "style.css"
    f.write_text(THEME + "\n" + OLD_BLOCK, encoding="utf-8")
    assert CSS.main(["x", str(f)]) == 0
    once = f.read_text(encoding="utf-8")
    assert (tmp_path / "style.css.prima-di-edith").read_text(encoding="utf-8") == THEME + "\n" + OLD_BLOCK
    assert CSS.main(["x", str(f)]) == 0 and f.read_text(encoding="utf-8") == once                # rilanciato: nessun doppione
    assert once.count("#custom-edith {") == 3 and once.count("#custom-edith:hover {") == 1 and once.count(CSS.MARKER) == 1
    g = tmp_path / "altro.css"
    g.write_text("window#waybar { background: black; }\n", encoding="utf-8")
    assert CSS.main(["x", str(g)]) == 2 and g.read_text(encoding="utf-8") == "window#waybar { background: black; }\n"
    assert CSS.main(["x", str(tmp_path / "manca.css")]) == 1
