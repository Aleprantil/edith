"""L'isola di EDITH per Hyprland (extras/hyprland/island): logica, rete, testo, cronologia, comandi locali."""
import importlib.machinery
import importlib.util
import io
import json
import os
import random
import socket
import stat
import sys
import time
import urllib.error
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parent.parent
ISLAND = ROOT / "desktop" / "island"
sys.path.insert(0, str(ISLAND))
import edith_core as C  # noqa: E402

TZ = ZoneInfo("Europe/Rome")
NOW = datetime(2026, 10, 6, 12, 50, tzinfo=TZ)


# ---------------------------------------------------------------------------
# Impostazioni e identità
# ---------------------------------------------------------------------------

def test_settings_files_env_and_validation(monkeypatch, tmp_path):
    monkeypatch.setattr(C, "CONFIG_DIR", tmp_path)
    for k in ("EDITH_URL", "EDITH_ISLAND_POSITION", "EDITH_ISLAND_WIDTH", "EDITH_NICKNAME"):
        monkeypatch.delenv(k, raising=False)
    assert C.settings()["EDITH_URL"] == C.DEFAULT_URL and C.settings()["EDITH_ISLAND_POSITION"] == "center"
    (tmp_path / "waybar.conf").write_text("EDITH_URL=https://dal-widget:8443/\n")
    assert C.settings()["EDITH_URL"] == "https://dal-widget:8443"                          # riusa il file del widget
    (tmp_path / "edith.conf").write_text("# commento\nEDITH_URL='https://mio:8443'\nEDITH_ISLAND_POSITION=boh\nEDITH_ISLAND_WIDTH=9999\n")
    s = C.settings()
    assert s["EDITH_URL"] == "https://mio:8443" and s["EDITH_ISLAND_POSITION"] == "center" and s["EDITH_ISLAND_WIDTH"] == "720"
    monkeypatch.setenv("EDITH_ISLAND_POSITION", "right")
    monkeypatch.setenv("EDITH_ISLAND_WIDTH", "abc")
    assert C.settings()["EDITH_ISLAND_POSITION"] == "right" and C.settings()["EDITH_ISLAND_WIDTH"] == "440"


@pytest.mark.parametrize("host", ["archlinux", "a", "PC di Mario.local", "x" * 90, "ä@!"])
def test_session_id_is_accepted_by_the_server(monkeypatch, host):
    import re
    monkeypatch.setattr(C.socket, "gethostname", lambda: host)
    assert re.fullmatch(r"[A-Za-z0-9_-]{8,64}", C.session_id()) and C.session_id().startswith("island-")


# ---------------------------------------------------------------------------
# Rete
# ---------------------------------------------------------------------------

class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def opener(responses, seen=None):
    def op(req, timeout=None):
        if seen is not None:
            seen.append({"url": req.full_url, "method": req.get_method(), "body": json.loads(req.data) if req.data else None,
                         "headers": {k.lower(): v for k, v in req.header_items()}, "timeout": timeout})
        r = responses(req) if callable(responses) else responses
        if isinstance(r, Exception):
            raise r
        return FakeResp(r if isinstance(r, bytes) else json.dumps(r).encode())
    return op


def http_error(code):
    return urllib.error.HTTPError("https://x", code, "err", {}, io.BytesIO(b""))


def test_ask_and_confirm_send_what_the_server_expects():
    seen = []
    c = C.Client("https://x/", "island-test", opener({"html": "ok", "ok": True}, seen))
    assert c.ask("  cosa   faccio\n oggi?  ") == {"html": "ok", "ok": True}
    c.confirm("tok", True)
    a, b = seen
    assert a["url"] == "https://x/api/assistant" and a["method"] == "POST" and a["body"] == {"text": "cosa faccio oggi?", "session": "island-test"}
    assert a["headers"]["x-requested-with"] == "edith" and a["headers"]["content-type"] == "application/json" and a["timeout"] == C.ASK_TIMEOUT
    assert b["url"] == "https://x/api/confirm" and b["body"] == {"session": "island-test", "token": "tok", "yes": True}
    c.ask("x" * 900)
    assert len(seen[-1]["body"]["text"]) == 500
    with pytest.raises(C.EdithError):
        c.ask("   ")


@pytest.mark.parametrize("exc,kind,word", [(http_error(401), C.AuthError, "Tailscale"), (http_error(403), C.AuthError, "WEB_ALLOWED_LOGINS"),
                                           (http_error(429), C.EdithError, "Troppe"), (http_error(500), C.EdithError, "500"),
                                           (urllib.error.URLError("giù"), C.OfflineError, "Tailscale"), (TimeoutError(), C.OfflineError, "non risponde"),
                                           (ConnectionRefusedError(), C.OfflineError, "non risponde")])
def test_network_errors_become_clear_messages(exc, kind, word):
    with pytest.raises(kind) as e:
        C.Client("https://x", "island-test", opener(exc)).ask("ciao")
    assert word in str(e.value)


def test_a_broken_response_is_an_error_not_a_crash():
    with pytest.raises(C.EdithError):
        C.Client("https://x", "island-test", opener(b"<html>non json")).ask("ciao")


def test_cards_survive_one_failure_and_surface_auth():
    def resp(req):
        if req.full_url.endswith("/tasks"):
            return urllib.error.URLError("lento")
        return {"ok": True, "data": {"n": req.full_url.rsplit("/", 1)[1]}}
    out = C.Client("https://x", "island-test", opener(resp)).cards()
    assert out == {"today": {"n": "today"}, "server": {"n": "server"}}
    with pytest.raises(C.AuthError):
        C.Client("https://x", "island-test", opener(http_error(401))).cards()
    with pytest.raises(C.OfflineError):
        C.Client("https://x", "island-test", opener(urllib.error.URLError("giù"))).cards()


def test_quotes_are_cached_for_a_day(monkeypatch, tmp_path):
    monkeypatch.setattr(C, "CACHE_DIR", tmp_path)
    seen = []
    c = C.Client("https://x", "island-test", opener({"frasi": [{"t": "Una"}, {"x": 1}, "rotta"]}, seen))
    assert c.quotes() == [{"t": "Una"}] and c.quotes() == [{"t": "Una"}] and len(seen) == 1
    assert stat.S_IMODE(os.stat(tmp_path / "frasi.json").st_mode) == 0o600
    off = C.Client("https://x", "island-test", opener(urllib.error.URLError("giù")))
    (tmp_path / "frasi.json").write_text(json.dumps({"at": time.time() - 2 * 86400, "frasi": [{"t": "Vecchia"}]}))
    assert off.quotes() == [{"t": "Vecchia"}]                                                   # senza rete: la copia vecchia va bene


# ---------------------------------------------------------------------------
# Testo per GTK
# ---------------------------------------------------------------------------

def pango_ok(markup: str) -> bool:
    gi = pytest.importorskip("gi")
    try:
        gi.require_version("Pango", "1.0")
        from gi.repository import Pango
    except (ValueError, ImportError):
        pytest.skip("Pango non disponibile")
    import re
    # I link li gestisce GTK (GtkLabel), non Pango: devono essere bilanciati e mai uno dentro l'altro; il resto lo controlla Pango.
    depth = 0
    for tag in re.findall(r"<(/?)a\b[^>]*>", markup):
        depth += -1 if tag else 1
        if depth not in (0, 1):
            return False
    if depth != 0 or re.search(r"<a\b(?![^>]*\bhref=\"https?://[^\"]*\">)[^>]*>", markup):
        return False
    try:
        Pango.parse_markup(re.sub(r"</?a\b[^>]*>", "", markup), -1, "\0")
        return True
    except Exception:
        return False


@pytest.mark.parametrize("src,expected", [
    ("📅 <b>Ecco</b><br>• 09:00 <i>Lezione</i> &amp; <code>lab</code>", "📅 <b>Ecco</b>\n• 09:00 <i>Lezione</i> &amp; <tt>lab</tt>"),
    ('<strong>Sì</strong> <em>no</em>', "<b>Sì</b> <i>no</i>"),
    ('<a href="https://nextcloud.it/x?a=1&amp;b=2" target="_blank">nota</a>', '<a href="https://nextcloud.it/x?a=1&amp;b=2">nota</a>'),
    ('<a href="javascript:alert(1)">clic</a>', "clic"),
    ("<b>aperto <i>dentro", "<b>aperto <i>dentro</i></b>"),
    ("<b>uno</i> due</b>", "<b>uno due</b>"),
    ("<script>x</script>2 < 3 > 1", "x2 &lt; 3 &gt; 1"),
    ("a<br><br><br><br>b", "a\n\nb"),
    ('<a href="https://a.it">uno <a href="https://b.it">due</a> tre</a>', '<a href="https://a.it">uno due tre</a>'),
])
def test_to_pango(src, expected):
    out = C.to_pango(src)
    assert out == expected and pango_ok(out)


def test_to_pango_never_produces_invalid_markup():
    rnd = random.Random(7)
    bits = ["<b>", "</b>", "<i>", "</i>", "<code>", "</code>", "<br>", "<a href='https://x.it'>", "</a>", "<a href=\"ftp://x\">", "<em>", "</strong>",
            "&amp;", "&", "<", ">", "\"", "'", "testo", "📅", " ", "<span foreground='red'>", "</span>", "<u>", "&lt;b&gt;"]
    for _ in range(400):
        s = "".join(rnd.choice(bits) for _ in range(rnd.randint(1, 14)))
        assert pango_ok(C.to_pango(s)), s


def test_plain():
    assert C.plain("📅 <b>Ecco</b><br>• Lezione &amp; lab") == "📅 Ecco\n• Lezione & lab"


# ---------------------------------------------------------------------------
# Cronologia locale
# ---------------------------------------------------------------------------

def test_history_persists_caps_and_is_private(tmp_path):
    p = tmp_path / "sub" / "h.json"
    h = C.History(p, cap=5)
    for i in range(8):
        h.add("me" if i % 2 == 0 else "edith", f"m{i}")
    assert [m["text"] for m in h.items] == ["m3", "m4", "m5", "m6", "m7"] and h.last_question() == "m6"
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert [m["text"] for m in C.History(p, cap=5).items] == ["m3", "m4", "m5", "m6", "m7"]
    h.clear()
    assert C.History(p).items == [] and h.last_question() is None


@pytest.mark.parametrize("content", ["{rotto", json.dumps({"a": 1}), json.dumps([{"who": "hacker", "text": "x"}, {"who": "me"}, 3, {"who": "me", "text": "ok"}])])
def test_a_damaged_history_file_is_ignored(tmp_path, content):
    p = tmp_path / "h.json"
    p.write_text(content)
    items = C.History(p).items
    assert all(m["who"] in ("me", "edith", "error") and isinstance(m["text"], str) for m in items)


# ---------------------------------------------------------------------------
# Comandi locali
# ---------------------------------------------------------------------------

@pytest.fixture
def sock_path(tmp_path):
    d = Path("/tmp") / f"edith-test-{os.getpid()}-{time.time_ns()}"              # i socket Unix vogliono un percorso corto
    d.mkdir(mode=0o700)
    yield d / "island.sock"
    for f in d.iterdir():
        f.unlink()
    d.rmdir()


def test_commands_reach_the_running_island(sock_path):
    got = []
    srv = C.CommandServer(got.append, sock_path)
    assert srv.start() is True
    try:
        assert stat.S_IMODE(os.stat(sock_path).st_mode) == 0o600
        for cmd in ("toggle", "show", "ask", "hide"):
            assert C.send_command(cmd, sock_path) is True
        assert got == ["toggle", "show", "ask", "hide"]
        with pytest.raises(ValueError):
            C.send_command("rm -rf /", sock_path)
        raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)                    # un altro programma che manda testo a caso
        raw.connect(str(sock_path))
        raw.sendall(b"; rm -rf ~\n")
        assert raw.recv(8) == b"no\n"
        raw.close()
        assert got == ["toggle", "show", "ask", "hide"]
        assert C.CommandServer(got.append, sock_path).start() is False                 # una sola isola alla volta
        assert got[-1] == "refresh"
    finally:
        srv.stop()
    assert not sock_path.exists() and C.send_command("show", sock_path) is False


def test_a_leftover_socket_from_a_crash_is_replaced(sock_path):
    dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dead.bind(str(sock_path))
    dead.close()                                                                    # file rimasto, nessuno in ascolto
    got = []
    srv = C.CommandServer(got.append, sock_path)
    try:
        assert srv.start() is True and C.send_command("show", sock_path) and got == ["show"]
    finally:
        srv.stop()


def test_socket_path_prefers_the_private_runtime_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert C.socket_path() == tmp_path / "edith-island.sock"
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    assert str(C.socket_path()).startswith("/tmp/edith-island-")


# ---------------------------------------------------------------------------
# Stesse regole del widget di Waybar
# ---------------------------------------------------------------------------

spec = importlib.util.spec_from_file_location("edith_waybar_for_island", ROOT / "desktop" / "edith-waybar.py")
WB = importlib.util.module_from_spec(spec)
spec.loader.exec_module(WB)
import test_waybar as TW  # noqa: E402


@pytest.mark.parametrize("hour,minute", [(8, 0), (12, 50), (14, 0), (16, 40), (20, 0), (23, 30)])
def test_same_next_event_and_flags_as_the_widget(hour, minute):
    now = NOW.replace(hour=hour, minute=minute)
    data = {"today": TW.TODAY, "tasks": TW.TASKS, "server": TW.SERVER}
    assert C.next_event(TW.TODAY, now) == WB.next_event(TW.TODAY, now)
    assert C.flags(data, now) == WB.flags(data, now)
    assert C.server_bad(TW.SERVER) == WB.server_bad(TW.SERVER)


def test_summary_and_quote():
    data = {"today": TW.TODAY, "tasks": TW.TASKS, "server": TW.SERVER}
    s = C.summary(data, NOW)
    assert s["next"]["title"] == "Robotics" and s["overdue"] == 1 and s["open"] == 2 and s["weather"] == "⛅ 9–19°" and s["status"] == "ok"
    assert C.summary({}, NOW) == {"next": None, "overdue": 0, "open": 0, "weather": "", "status": "ok"}
    pool = TW.POOL
    f = C.flags(data, NOW)
    for seed in range(80):
        q = next(x for x in pool if x["t"] == C.pick_quote(pool, data, NOW, seed))
        assert all(f.get(k) for k in q.get("c", []))                                       # mai una frase fuori contesto
    assert C.pick_quote([], data, NOW) is None


# ---------------------------------------------------------------------------
# Il comando edith-island
# ---------------------------------------------------------------------------

def load_cli():
    loader = importlib.machinery.SourceFileLoader("edith_island_cli", str(ISLAND / "edith-island"))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("edith_island_cli", loader))
    loader.exec_module(mod)
    return mod


def test_cli_sends_to_a_running_island_or_starts_one(monkeypatch, capsys):
    cli = load_cli()
    sent, spawned = [], []
    monkeypatch.setattr(cli, "spawn", spawned.append)
    monkeypatch.setattr(cli.core, "send_command", lambda cmd, *a, **k: (sent.append(cmd), True)[1])
    assert cli.main(["edith-island"]) == 0 and sent == ["toggle"] and spawned == []
    assert cli.main(["edith-island", "ask"]) == 0 and sent[-1] == "ask"
    monkeypatch.setattr(cli.core, "send_command", lambda cmd, *a, **k: False)            # nessuna isola aperta
    for cmd, start in (("toggle", "show"), ("show", "show"), ("ask", "ask"), ("hide", None), ("quit", None), ("refresh", None)):
        spawned.clear()
        assert cli.main(["edith-island", cmd]) == 0 and spawned == ([start] if start else [])
    assert cli.main(["edith-island", "rm"]) == 2 and "Comando sconosciuto" in capsys.readouterr().err
    assert cli.main(["edith-island", "--help"]) == 0 and "toggle" in capsys.readouterr().out


def test_cli_preloads_layer_shell_only_on_wayland(monkeypatch):
    cli = load_cli()
    calls = []
    monkeypatch.setattr(cli, "layer_lib", lambda: "/usr/lib/libgtk4-layer-shell.so.0")
    monkeypatch.setattr(cli.os, "execve", lambda exe, args, env: calls.append((args, env.get("LD_PRELOAD"))) or (_ for _ in ()).throw(SystemExit(0)))
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    monkeypatch.delenv("LD_PRELOAD", raising=False)
    with pytest.raises(SystemExit):
        cli.daemon("show")
    assert calls[0][1] == "/usr/lib/libgtk4-layer-shell.so.0" and calls[0][0][-2:] == ["--daemon", "show"]


def test_ui_module_follows_the_rules():
    import re
    src = (ISLAND / "edith_island_ui.py").read_text(encoding="utf-8")
    assert "urllib" not in src and "requests" not in src                                  # la rete passa solo da edith_core
    assert "in_thread(" in src and "GLib.idle_add" in src                                  # mai rete nel thread della grafica
    # tutto ciò che finisce nel fumetto passa da una funzione che lo ripulisce
    allowed = (r"^strongify\(core\.to_pango\(", r"^GLib\.markup_escape_text\(", r"^strongify\(text\)$", r"^text$",
               r"^'<span foreground=\"#b85700\" weight=\"bold\">●  ●  ●</span>'$",
               r"^f\"Ciao\{\(' ' \+ GLib\.markup_escape_text\(nick\)\) if nick else ''\}")
    calls = re.findall(r"(?:self|app)\.(speak|present)\(([^,\n]+?)(?:,|\)$|\)\n)", src)
    assert len(calls) >= 12
    for fn, arg in calls:
        if fn == "speak" and arg.strip() == "markup":
            continue                                                               # passaggio da present(): controllato sotto
        assert any(re.search(a, arg.strip()) for a in allowed), (fn, arg)
    body = src[src.index("    def present("):src.index("    def end_voice(")]
    assert src.count("self.speak(markup") == body.count("self.speak(markup") >= 2        # «markup» va nel fumetto solo da present()
    assert re.search(r"text = strongify\(core\.to_pango\(p\[\"a\"\]\)\) if p\[\"who\"\] == \"edith\" else GLib\.markup_escape_text\(core\.plain", src)
    assert re.search(r"text = core\.idle_line\(", src) and "GLib.markup_escape_text(core.greeting(" in src
    for m in re.findall(r"set_markup\(([^)]*\)?)", src):
        assert m.startswith(("markup", "core.markup_prefix(", 'tw["markup"]')), m
    css = (ISLAND / "style.css").read_text(encoding="utf-8")
    assert css.count("{") == css.count("}")



@pytest.mark.parametrize("hour,start", [(8, "Buongiorno, Mario!"), (13, "Ciao, Mario!"), (20, "Buonasera, Mario!"), (23, "Ancora sveglio, Mario?"), (3, "Ancora sveglio, Mario?")])
def test_greeting_depends_on_the_time(hour, start):
    g = C.greeting(NOW.replace(hour=hour), "Mario", 0)
    assert g.startswith(start) and g.endswith(C.GREETINGS_EXTRA[0])
    assert {C.greeting(NOW, "Mario", n) for n in range(len(C.GREETINGS_EXTRA))} == {f"Ciao, Mario! {x}" for x in C.GREETINGS_EXTRA}


def test_the_character_only_animates_while_the_island_is_open():
    src = (ISLAND / "edith_island_ui.py").read_text(encoding="utf-8")
    assert "class Avatar(Gtk.DrawingArea)" in src and "add_tick_callback" in src and "remove_tick_callback" in src
    assert "self.avatar.start()" in src and "self.avatar.stop()" in src
    assert 'core.greeting(datetime.now(), self.conf["EDITH_NICKNAME"])' in src


# ---------------------------------------------------------------------------
# Il personaggio che parla
# ---------------------------------------------------------------------------

RICH = '📅 <b>Ecco</b> cosa ho trovato:\n• <span weight="bold" foreground="#b85700">Robotics</span> alle 13:30 &amp; <i>lab</i> <a href="https://x.it">qui</a>'


def test_markup_prefix_is_always_valid_and_grows_one_visible_char_at_a_time():
    total = C.visible_len(RICH)
    assert total == len(C.plain(RICH.replace('<span weight="bold" foreground="#b85700">', "").replace("</span>", "")).replace("\n", "\n"))
    prev = ""
    for n in range(0, total + 3):
        out = C.markup_prefix(RICH, n)
        assert pango_ok(out), (n, out)
        vis = C.visible_len(out)
        assert vis == min(n, total) and vis >= C.visible_len(prev)
        prev = out
    assert C.markup_prefix(RICH, total) == RICH
    assert C.markup_prefix(RICH, 3) == "📅 <b>E</b>"
    assert C.markup_prefix("", 5) == "" and C.visible_len("&amp;&lt;") == 2


def test_agenda_answers_are_recognised():
    assert C.is_agenda("📅 <b>Ecco cosa ho trovato</b>") and C.is_agenda("  📅 Oggi non hai impegni")
    assert not C.is_agenda("☑️ Promemoria aggiunto") and not C.is_agenda("")


@pytest.mark.parametrize("nxt,overdue,expected", [
    ({"when": "tra 41 min", "title": "Robotics"}, 1, "Il prossimo impegno è <b>Robotics</b>, tra 41 min. Hai un promemoria scaduto."),
    ({"when": "alle 21:00", "title": "Cena"}, 0, "Il prossimo impegno è <b>Cena</b> alle 21:00."),
    ({"when": "domani 08:30", "title": "Lab <3 & co"}, 3, "Il prossimo impegno è <b>Lab &lt;3 &amp; co</b>, domani 08:30. Hai 3 promemoria scaduti."),
    ({"when": "in corso", "title": "Robotics", "extra": "fino alle 16:30", "now": True}, 0, "Adesso sei a <b>Robotics</b>, fino alle 16:30."),
    (None, 0, "Nessun impegno in vista: giornata libera."),
])
def test_idle_line(nxt, overdue, expected):
    out = C.idle_line({"next": nxt, "overdue": overdue, "open": 2})
    assert out == expected and pango_ok(out)


def test_timeline_states_and_positions():
    tl = C.timeline(TW.TODAY, NOW)                                                    # 12:50: lezione finita, Robotics alle 13:30, allenamento 17:50
    assert [(p["label"], p["state"]) for p in tl["points"]] == [("09:00", "done"), ("13:30", "next"), ("17:50", "later")]
    assert tl["points"][0]["x"] == 0.0 and 0.4 < tl["points"][1]["x"] < 0.5 and tl["points"][2]["x"] < 1.0 and 0.0 < tl["now_x"] < tl["points"][1]["x"]
    during = C.timeline(TW.TODAY, NOW.replace(hour=14))
    assert [p["state"] for p in during["points"]] == ["done", "now", "later"]
    before, after = C.timeline(TW.TODAY, NOW.replace(hour=7)), C.timeline(TW.TODAY, NOW.replace(hour=22))
    assert before["now_x"] == 0.0 and [p["state"] for p in before["points"]] == ["next", "later", "later"]
    assert after["now_x"] == 1.0 and {p["state"] for p in after["points"]} == {"done"}
    one = C.timeline({"events": [TW.ev("Solo", 15)]}, NOW)
    assert one["points"][0]["x"] == 0.5 and one["points"][0]["state"] == "next"
    assert C.timeline({"events": [{"title": "Tutto il giorno", "start_iso": None, "end_iso": None}]}, NOW) is None and C.timeline({}, NOW) is None


def test_history_pairs():
    items = [{"who": "me", "text": "a"}, {"who": "edith", "text": "A"}, {"who": "edith", "text": "ciao"}, {"who": "me", "text": "b"}, {"who": "error", "text": "B!"}, {"who": "me", "text": "c"}]
    assert C.history_pairs(items) == [{"q": "a", "a": "A", "who": "edith"}, {"q": None, "a": "ciao", "who": "edith"}, {"q": "b", "a": "B!", "who": "error"}]
