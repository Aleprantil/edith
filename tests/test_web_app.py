"""Accesso, difese e rotte della web app (assistente e schede finti)."""
import time

import pytest
from starlette.testclient import TestClient

from services.assistant import Reply
from web import cards, security
from web.app import create_app

LOGIN = {"Tailscale-User-Login": "me@example.com"}
API = {"X-Requested-With": "edith", "Content-Type": "application/json"}
SID = "session-abcdef123"


class FakeAssistant:
    def __init__(self):
        self.calls = []
        self.spoken = []
        self.voice = []

    def handle(self, sid, text, spoken=False):
        self.calls.append((sid, text))
        self.spoken.append(spoken)
        return Reply(html=f"ricevuto <b>{text}</b>", refresh=["today"], speak="ok")

    def confirm(self, sid, token, yes, spoken=False):
        self.calls.append(("confirm", token, yes))
        self.spoken.append(spoken)
        return Reply(html="fatto" if yes else "annullato")

    def handle_voice(self, sid, wav):
        self.voice.append((sid, len(wav)))
        return Reply(html="ok voce", speak="Ho capito.", heard="cosa faccio oggi")

    def transcribe(self, wav):
        return "sì"


def make(password="", logins=("me@example.com",), secret=b"s3cret"):
    st = security.WebSettings(allowed_logins=list(logins), password=password, secret=secret, assistant_name="EDITH", owner_name="Tester")
    asst = FakeAssistant()
    return TestClient(create_app(st, asst), base_url="http://testserver"), st, asst


# ---------------------------------------------------------------------------
# Chi può entrare
# ---------------------------------------------------------------------------

def test_allowed_login_sees_the_app():
    c, *_ = make()
    r = c.get("/", headers=LOGIN)
    assert r.status_code == 200 and "orb-cv" in r.text and "{{NAME}}" not in r.text


def test_login_is_case_insensitive():
    c, *_ = make()
    assert c.get("/api/me", headers={"Tailscale-User-Login": "ME@Example.COM"}).status_code == 200


def test_unknown_login_is_refused_and_told_how_to_fix():
    c, *_ = make()
    r = c.get("/", headers={"Tailscale-User-Login": "intruso@example.com"})
    assert r.status_code == 403
    assert "intruso@example.com" in r.text and "WEB_ALLOWED_LOGINS" in r.text
    assert c.get("/api/me", headers={"Tailscale-User-Login": "intruso@example.com"}).status_code == 401


def test_no_identity_and_no_password_is_refused_with_instructions():
    c, *_ = make()
    r = c.get("/")
    assert r.status_code == 401 and "Tailscale" in r.text
    assert c.get("/api/cards/today").status_code == 401


def test_unconfigured_server_shows_setup_page_and_logs_in_nobody():
    c, *_ = make(logins=())
    r = c.get("/", headers={"Tailscale-User-Login": "me@example.com"})
    assert r.status_code == 403 and "WEB_ALLOWED_LOGINS=me@example.com" in r.text
    assert c.get("/api/me", headers=LOGIN).status_code == 401


def test_login_in_page_is_html_escaped():
    c, *_ = make()
    r = c.get("/", headers={"Tailscale-User-Login": "<script>alert(1)</script>@x.it"})
    assert "<script>alert(1)" not in r.text and "&lt;script&gt;" in r.text


def test_healthz_is_open_and_reveals_nothing():
    c, *_ = make()
    r = c.get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}


# ---------------------------------------------------------------------------
# Password
# ---------------------------------------------------------------------------

def test_password_login_flow_and_cookie():
    c, *_ = make(password="giusta", logins=())
    assert c.get("/api/me").status_code == 401
    assert c.post("/api/login", json={"password": "sbagliata"}, headers=API).status_code == 401
    r = c.post("/api/login", json={"password": "giusta"}, headers=API)
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert c.get("/api/me").status_code == 200           # il cookie è nel client di prova
    assert c.get("/").status_code == 200


def test_login_page_has_form_only_when_password_is_set():
    with_pw, *_ = make(password="x", logins=())
    without, *_ = make()
    assert 'id="loginform"' in with_pw.get("/").text
    assert 'id="loginform"' not in without.get("/").text
    assert without.post("/api/login", json={"password": "x"}, headers=API).status_code == 404


def test_too_many_wrong_passwords_lock_out_even_the_right_one():
    c, *_ = make(password="giusta", logins=())
    for _ in range(5):
        assert c.post("/api/login", json={"password": "no"}, headers=API).status_code == 401
    assert c.post("/api/login", json={"password": "giusta"}, headers=API).status_code == 429


def test_cookie_cannot_be_forged_or_reused_after_expiry_or_without_password_mode():
    st = security.WebSettings(password="p", secret=b"k")
    good = security.make_cookie(st, now=1000)
    assert security.check_cookie(st, good, now=1001)
    assert not security.check_cookie(st, good, now=1000 + security.COOKIE_TTL + 1)
    assert not security.check_cookie(st, good[:-3] + "abc", now=1001)
    assert not security.check_cookie(security.WebSettings(password="p", secret=b"altra"), good, now=1001)
    assert not security.check_cookie(security.WebSettings(password="", secret=b"k"), good, now=1001)   # senza password i cookie non valgono
    assert not security.check_cookie(st, "", now=1001) and not security.check_cookie(st, "a.b", now=1001)


def test_secret_is_derived_from_bot_token_when_not_set():
    assert security.make_secret("", "tok") == security.make_secret("", "tok") != security.make_secret("", "altro")
    assert security.make_secret("mio", "tok") == b"mio"


# ---------------------------------------------------------------------------
# Difese contro richieste di altri siti e dati malformati
# ---------------------------------------------------------------------------

def post(c, path, body, headers=None, **kw):
    return c.post(path, json=body, headers={**LOGIN, **API, **(headers or {})}, **kw)


def test_assistant_requires_login_and_our_own_header():
    c, _, asst = make()
    assert c.post("/api/assistant", json={"text": "ciao", "session": SID}, headers=API).status_code == 401
    h = {**LOGIN, "Content-Type": "application/json"}                                  # manca X-Requested-With
    assert c.post("/api/assistant", json={"text": "ciao", "session": SID}, headers=h).status_code == 400
    assert asst.calls == []


def test_cross_site_requests_are_refused_even_with_valid_identity():
    c, _, asst = make()
    for site in ("cross-site", "same-site"):
        assert post(c, "/api/assistant", {"text": "cancella tutto", "session": SID}, {"Sec-Fetch-Site": site}).status_code == 400
    assert post(c, "/api/assistant", {"text": "ok", "session": SID}, {"Sec-Fetch-Site": "same-origin"}).status_code == 200
    assert [t for _, t in asst.calls] == ["ok"]


def test_only_json_is_accepted():
    c, *_ = make()
    r = c.post("/api/assistant", content="text=ciao", headers={**LOGIN, "X-Requested-With": "edith", "Content-Type": "text/plain"})
    assert r.status_code == 400


@pytest.mark.parametrize("body", [{"text": "ciao"}, {"session": SID}, {"text": 5, "session": SID}, {"text": "x", "session": "corta"},
                                  {"text": "x", "session": "../../etc/passwd"}, {"text": "x", "session": "a" * 65}, []])
def test_malformed_assistant_requests_are_rejected(body):
    c, _, asst = make()
    assert post(c, "/api/assistant", body).status_code == 400 and asst.calls == []


def test_oversized_body_is_rejected():
    c, _, asst = make()
    r = c.post("/api/assistant", content=b'{"text": "' + b"a" * 20000 + b'", "session": "' + SID.encode() + b'"}', headers={**LOGIN, **API})
    assert r.status_code == 400 and asst.calls == []


def test_invalid_json_is_rejected_not_crashed():
    c, *_ = make()
    r = c.post("/api/assistant", content=b"{non json", headers={**LOGIN, **API})
    assert r.status_code == 400


def test_assistant_and_confirm_happy_path():
    c, _, asst = make()
    r = post(c, "/api/assistant", {"text": "ciao", "session": SID})
    assert r.status_code == 200 and r.json()["html"].startswith("ricevuto") and r.json()["refresh"] == ["today"]
    r = post(c, "/api/confirm", {"session": SID, "token": "abc", "yes": True})
    assert r.status_code == 200 and r.json()["html"] == "fatto"
    assert post(c, "/api/confirm", {"session": SID, "token": "abc", "yes": "si"}).status_code == 400    # deve essere un booleano
    assert post(c, "/api/confirm", {"session": SID, "yes": True}).status_code == 400


def test_responses_are_never_cached_and_carry_security_headers():
    c, *_ = make()
    for r in (c.get("/api/me", headers=LOGIN), c.get("/", headers=LOGIN), c.get("/healthz")):
        assert "no-store" in r.headers["cache-control"] or "no-cache" in r.headers["cache-control"]
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"] and "script-src 'self'" in r.headers["content-security-policy"]
        assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
        assert r.headers["referrer-policy"] == "no-referrer"


def test_page_has_no_inline_script_or_style_so_csp_can_stay_strict():
    html = TestClient(create_app(security.WebSettings(allowed_logins=["me@example.com"], secret=b"k"), FakeAssistant())).get("/", headers=LOGIN).text
    assert "<script>" not in html and " style=" not in html and "onclick=" not in html
    assert html.count("<script") == 1 and 'src="/static/app.js"' in html


# ---------------------------------------------------------------------------
# File statici
# ---------------------------------------------------------------------------

def test_static_files_are_served_and_typed():
    c, *_ = make()
    assert c.get("/static/app.js").status_code == 200 and "javascript" in c.get("/static/app.js").headers["content-type"]
    assert c.get("/static/apple-touch-icon.png").headers["content-type"] == "image/png"
    font = c.get("/static/sg-300.woff2")
    assert font.status_code == 200 and font.headers["content-type"] == "font/woff2" and "immutable" in font.headers["cache-control"]
    assert "no-cache" in c.get("/static/app.js").headers["cache-control"]               # il codice, invece, si ricontrolla sempre


@pytest.mark.parametrize("path", ["/static/nonesiste.js", "/static/..%2Fapp.py", "/static/%2e%2e%2f%2e%2e%2fconfig.py", "/static/..%5Cconfig.py",
                                  "/static/%2Fetc%2Fpasswd", "/static/.env"])
def test_static_path_traversal_is_blocked(path):
    c, *_ = make()
    r = c.get(path)
    assert r.status_code == 404 and "TELEGRAM_BOT_TOKEN" not in r.text and "root:" not in r.text


# ---------------------------------------------------------------------------
# Schede
# ---------------------------------------------------------------------------

def test_cards_require_login_and_unknown_card_is_404(monkeypatch):
    c, *_ = make()
    assert c.get("/api/cards/today").status_code == 401
    assert c.get("/api/cards/segreto", headers=LOGIN).status_code == 404
    assert c.get("/api/cards/../../etc/passwd", headers=LOGIN).status_code in (404,)


def test_a_failing_card_reports_error_without_breaking_the_others(monkeypatch):
    c, *_ = make()
    cards.clear_cache()
    monkeypatch.setitem(cards.CARDS, "today", (lambda: (_ for _ in ()).throw(RuntimeError("calendario giù")), 0))
    monkeypatch.setitem(cards.CARDS, "notes", (lambda: {"items": [], "total": 0}, 0))
    bad = c.get("/api/cards/today", headers=LOGIN).json()
    good = c.get("/api/cards/notes", headers=LOGIN).json()
    assert bad["ok"] is False and "calendario giù" in bad["error"] and good["ok"] is True


def test_unavailable_card_is_flagged(monkeypatch):
    c, *_ = make()
    cards.clear_cache()
    monkeypatch.setitem(cards.CARDS, "finance", (lambda: (_ for _ in ()).throw(cards.CardUnavailable("non configurate")), 0))
    r = c.get("/api/cards/finance", headers=LOGIN).json()
    assert r == {"ok": False, "unavailable": True, "error": "non configurate"}


def test_card_cache_and_forced_refresh(monkeypatch):
    c, *_ = make()
    cards.clear_cache()
    n = {"v": 0}

    def fn():
        n["v"] += 1
        return {"n": n["v"]}
    monkeypatch.setitem(cards.CARDS, "server", (fn, 60))
    assert c.get("/api/cards/server", headers=LOGIN).json()["data"]["n"] == 1
    assert c.get("/api/cards/server", headers=LOGIN).json()["data"]["n"] == 1          # dalla cache
    assert c.get("/api/cards/server?refresh=1", headers=LOGIN).json()["data"]["n"] == 2


def test_me_endpoint_reports_name_and_card_order():
    c, *_ = make()
    j = c.get("/api/me", headers=LOGIN).json()
    assert j["assistant"] == "EDITH" and j["owner"] == "Tester" and j["cards"] == cards.ORDER


# ---------------------------------------------------------------------------
# Rinforzi: limiti, tempi, header fidato
# ---------------------------------------------------------------------------

def test_assistant_requests_are_rate_limited():
    c, _, asst = make()
    codes = [post(c, "/api/assistant", {"text": f"m{i}", "session": SID}).status_code for i in range(34)]
    assert codes[:30] == [200] * 30 and set(codes[30:]) == {429} and len(asst.calls) == 30


def test_rate_limiter_window_slides():
    rl = security.RateLimiter(2, 10)
    assert rl.hit(0) and rl.hit(1) and not rl.hit(2) and rl.hit(11.5)


def test_slow_assistant_gets_a_clear_timeout_instead_of_hanging(monkeypatch):
    import web.app as webapp
    monkeypatch.setattr(webapp, "ASSISTANT_TIMEOUT", 0.2)
    c, _, asst = make()
    asst.handle = lambda sid, text, spoken=False: (time.sleep(1.0), Reply(html="tardi"))[1]
    r = post(c, "/api/assistant", {"text": "ciao", "session": SID})
    assert r.status_code == 200 and r.json()["ok"] is False and "troppo" in r.json()["html"]


def test_tailscale_header_is_ignored_when_the_server_is_not_local():
    st = security.WebSettings(allowed_logins=["me@example.com"], password="pw", secret=b"k", trust_header=False)
    c = TestClient(create_app(st, FakeAssistant()))
    assert c.get("/api/me", headers=LOGIN).status_code == 401                          # header falsificabile: non basta
    assert c.post("/api/login", json={"password": "pw"}, headers=API).status_code == 200
    assert c.get("/api/me").status_code == 200                                         # con la password sì


def test_server_marks_non_local_binding_as_untrusted(monkeypatch):
    import config
    from web import server
    monkeypatch.setattr(config, "WEB_HOST", "0.0.0.0")
    assert server.build_settings().trust_header is False
    monkeypatch.setattr(config, "WEB_HOST", "127.0.0.1")
    assert server.build_settings().trust_header is True


def test_domain_errors_are_shown_without_the_technical_class_name(monkeypatch):
    from services.news_service import NewsError
    c, *_ = make()
    cards.clear_cache()
    monkeypatch.setitem(cards.CARDS, "news", (lambda: (_ for _ in ()).throw(NewsError("Nextcloud News non risponde")), 0))
    assert c.get("/api/cards/news", headers=LOGIN).json()["error"] == "Nextcloud News non risponde"
    monkeypatch.setitem(cards.CARDS, "news", (lambda: (_ for _ in ()).throw(KeyError("x")), 0))
    assert c.get("/api/cards/news?refresh=1", headers=LOGIN).json()["error"].startswith("KeyError")


# ---------------------------------------------------------------------------
# Nome dell'assistente (EDITH) inserito dal server
# ---------------------------------------------------------------------------

def named(name):
    st = security.WebSettings(allowed_logins=["me@example.com"], secret=b"k", assistant_name=name, owner_name="Tester")
    return TestClient(create_app(st, FakeAssistant()))


def test_assistant_name_is_inserted_in_page_and_manifest_and_escaped():
    c = named("EDITH")
    page = c.get("/", headers=LOGIN).text
    assert "<title>EDITH</title>" in page and 'content="EDITH"' in page and "{{NAME}}" not in page
    m = c.get("/manifest.webmanifest")                                              # pubblico: il browser lo chiede senza identità
    assert m.status_code == 200 and "manifest+json" in m.headers["content-type"]
    j = m.json()
    assert j["name"] == "EDITH" and j["display"] == "standalone" and j["icons"] and j["theme_color"] == "#070605"
    evil = named('<img src=x onerror=alert(1)>"')
    page = evil.get("/", headers=LOGIN).text
    assert "<img src=x" not in page and "&lt;img" in page
    assert len(named("UnNomeMoltoLungoDavvero").get("/manifest.webmanifest").json()["short_name"]) <= 12


def test_default_name_is_edith():
    assert security.WebSettings().assistant_name == "EDITH"
