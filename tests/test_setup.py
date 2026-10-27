"""La configurazione guidata (edith-setup): file .env, verifiche, e un giro completo con risposte simulate."""
import importlib.machinery
import importlib.util
import io
import json
import os
import stat
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
loader = importlib.machinery.SourceFileLoader("edith_setup", str(ROOT / "edith-setup"))
S = importlib.util.module_from_spec(importlib.util.spec_from_loader("edith_setup", loader))
loader.exec_module(S)


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def opener(result):
    def op(req, timeout=None):
        if isinstance(result, Exception):
            raise result
        body = result(req) if callable(result) else result
        return Resp(body if isinstance(body, bytes) else json.dumps(body).encode())
    return op


def http_error(code):
    return urllib.error.HTTPError("https://x", code, "err", {}, io.BytesIO(b""))


# ---------------------------------------------------------------------------
# Il file .env
# ---------------------------------------------------------------------------

def test_env_roundtrip_is_private_and_keeps_a_backup(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# vecchio\nNEWS_TIME=12:00\nOWNER_NAME='Mario'\nCUSTOM=\"con # cancelletto\"\n")
    v = S.read_env(p)
    assert v == {"NEWS_TIME": "12:00", "OWNER_NAME": "Mario", "CUSTOM": "con # cancelletto"}
    v.update(TELEGRAM_BOT_TOKEN="123:abc", WEB_PASSWORD=" spazi ", ASSISTANT_NAME="EDITH", EMPTY="")
    backup = S.write_env(p, v)
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600 and stat.S_IMODE(os.stat(backup).st_mode) == 0o600
    assert backup.read_text().startswith("# vecchio")
    again = S.read_env(p)
    assert again["WEB_PASSWORD"] == " spazi " and again["CUSTOM"] == "con # cancelletto" and again["NEWS_TIME"] == "12:00"
    text = p.read_text()
    assert "EMPTY" not in text and text.index("# --- Base") < text.index("# --- Telegram") < text.index("# --- Altre impostazioni")


def test_the_example_file_reads_cleanly_without_inline_comments():
    v = S.read_env(ROOT / ".env.example")
    assert v["OWNER_NICKNAME"] == "Mario" and v["WEB_HOST"] == "127.0.0.1" and v["ADMIN_IDS"] == "" and v["CHARTS_FOLDER"] == "Grafici EDITH"
    assert not any("#" in x for x in v.values())
    assert "123456789:il-token" in v["TELEGRAM_BOT_TOKEN"] and "esempio" in v["CALDAV_URL"]


@pytest.mark.parametrize("src,expected", [("cloud.esempio.it", "https://cloud.esempio.it/remote.php/dav"),
                                          ("https://cloud.esempio.it/", "https://cloud.esempio.it/remote.php/dav"),
                                          ("https://x.it/remote.php/dav/", "https://x.it/remote.php/dav")])
def test_dav_url(src, expected):
    assert S.dav_url(src) == expected


# ---------------------------------------------------------------------------
# Verifiche
# ---------------------------------------------------------------------------

def test_telegram_checks():
    assert S.check_telegram("t", opener({"ok": True, "result": {"username": "EdithBot"}})) == "@EdithBot"
    for err, word in ((http_error(401), "token"), (urllib.error.URLError("x"), "internet")):
        with pytest.raises(S.CheckError) as e:
            S.check_telegram("t", opener(err))
        assert word in str(e.value)
    updates = {"result": [{"message": {"chat": {"id": -100, "type": "group"}}},
                          {"message": {"chat": {"id": 4242, "type": "private", "first_name": "Mario"}}}]}
    assert S.find_chat_id("t", opener(updates)) == ("4242", "Mario")
    assert S.find_chat_id("t", opener({"result": []})) == (None, None)


@pytest.mark.parametrize("result,ok,word", [({"candidates": []}, True, "risponde"), (http_error(429), True, "occupato"),
                                             (http_error(403), False, "chiave"), (http_error(404), False, "non esiste"),
                                             (urllib.error.URLError("x"), False, "internet")])
def test_gemini_check(result, ok, word):
    if ok:
        assert word in S.check_gemini("k", "gemini-2.5-flash", opener(result))
    else:
        with pytest.raises(S.CheckError) as e:
            S.check_gemini("k", "gemini-2.5-flash", opener(result))
        assert word in str(e.value)


def test_gemini_check_sends_the_key_in_a_header_not_in_the_url():
    seen = {}

    def capture(req):
        seen.update(url=req.full_url, headers={k.lower(): v for k, v in req.header_items()})
        return {"candidates": []}
    S.check_gemini("segreta", "gemini-2.5-flash", opener(capture))
    assert "segreta" not in seen["url"] and seen["headers"]["x-goog-api-key"] == "segreta"


def test_geocode():
    r = {"results": [{"name": "Trieste", "latitude": 45.64953, "longitude": 13.77678, "country": "Italia"}]}
    assert S.geocode("trieste", opener(r)) == ("Trieste", 45.6495, 13.7768, "Italia")
    with pytest.raises(S.CheckError):
        S.geocode("nessunposto", opener({}))


DAV = b'''<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
 <d:response><d:href>/remote.php/dav/calendars/mario/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat></d:response>
 <d:response><d:href>/remote.php/dav/calendars/mario/personal/</d:href><d:propstat><d:prop><d:displayname>Personale</d:displayname>
   <d:resourcetype><d:collection/><c:calendar/></d:resourcetype>
   <c:supported-calendar-component-set><c:comp name="VEVENT"/><c:comp name="VTODO"/></c:supported-calendar-component-set></d:prop></d:propstat></d:response>
 <d:response><d:href>/remote.php/dav/calendars/mario/todo/</d:href><d:propstat><d:prop><d:displayname>Da fare</d:displayname>
   <d:resourcetype><d:collection/><c:calendar/></d:resourcetype>
   <c:supported-calendar-component-set><c:comp name="VTODO"/></c:supported-calendar-component-set></d:prop></d:propstat></d:response>
</d:multistatus>'''


def test_nextcloud_check_lists_calendars_and_task_lists():
    seen = {}

    def capture(req):
        seen.update(method=req.get_method(), url=req.full_url, auth=req.get_header("Authorization"), depth=req.get_header("Depth"))
        return DAV
    cals = S.check_nextcloud("https://cloud.x/remote.php/dav", "mario", "pw", opener(capture))
    assert cals == [("Personale", True, True), ("Da fare", False, True)]
    assert seen["method"] == "PROPFIND" and seen["url"].endswith("/calendars/mario/") and seen["auth"].startswith("Basic ") and seen["depth"] == "1"
    for err, word in ((http_error(401), "password per app"), (http_error(404), "404"), (urllib.error.URLError("x"), "non risponde")):
        with pytest.raises(S.CheckError) as e:
            S.check_nextcloud("https://x", "u", "p", opener(err))
        assert word in str(e.value)
    with pytest.raises(S.CheckError):
        S.check_nextcloud("https://x", "u", "p", opener(b"<html>non xml"))


# ---------------------------------------------------------------------------
# Un giro completo, con le risposte di una persona simulata
# ---------------------------------------------------------------------------

def script(monkeypatch, answers):
    it = iter(answers)
    asked = []

    def fake_input(prompt=""):
        asked.append(prompt)
        try:
            return next(it)
        except StopIteration:
            raise AssertionError(f"domanda in più: {prompt}")
    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr(S.getpass, "getpass", fake_input)
    monkeypatch.setattr(S, "check_telegram", lambda t: "@EdithBot")
    monkeypatch.setattr(S, "find_chat_id", lambda t: ("4242", "Mario"))
    monkeypatch.setattr(S, "check_gemini", lambda k, m: "risponde")
    monkeypatch.setattr(S, "check_nextcloud", lambda u, us, p: [("Personale", True, True), ("Calcio", True, False), ("Da fare", False, True)])
    monkeypatch.setattr(S, "geocode", lambda c: ("Trieste", 45.6495, 13.7768, "Italia"))
    return asked, it


def test_full_run_with_nextcloud_weather_and_passions(monkeypatch, tmp_path):
    asked, it = script(monkeypatch, [
        "", "Mario", "Mari",                         # nome dell'assistente (Invio), nome, soprannome
        "123:abc", "",                               # token, poi Invio dopo /start
        "",                                          # ID Telegram trovato: confermo
        "chiave-gemini", "",                         # chiave, modello di serie
        "",                                          # riserva di serie
        "", "mario@gmail.com",                       # pagina web sì, account Tailscale
        "1,2,5,8",                                   # moduli: calendario, promemoria, meteo, server
        "cloud.esempio.it", "mario", "pw-app",       # Nextcloud
        "",                                          # calendari proposti: confermo
        "2",                                         # lista dei promemoria: «Da fare»
        "trieste",                                   # città
        "4,5",                                       # passioni: calcio e sci
    ])
    v = S.run(tmp_path / ".env")
    assert next(it, None) is None                    # tutte le risposte usate, nessuna domanda in più
    assert v["ASSISTANT_NAME"] == "EDITH" and v["OWNER_NAME"] == "Mario" and v["OWNER_NICKNAME"] == "Mari"
    assert v["TELEGRAM_CHAT_ID"] == "4242" and v["GEMINI_MODEL"] == "gemini-2.5-flash" and v["GEMINI_FALLBACK_MODEL"] == "gemini-flash-latest"
    assert v["EDITH_PLUGINS"] == "calendar,tasks,weather,server"
    assert v["CALDAV_URL"] == "https://cloud.esempio.it/remote.php/dav" and v["CALENDAR_CATEGORIES"] == "Personale,Calcio"
    assert v["TASKS_DEFAULT_LIST"] == "Da fare" and (v["WEATHER_PLACE"], v["WEATHER_LAT"]) == ("Trieste", "45.6495")
    assert v["EDITH_QUOTE_PACKS"] == "calcio,sci" and len(v["WEB_SECRET"]) == 64
    S.write_env(tmp_path / ".env", v)
    assert S.read_env(tmp_path / ".env")["CALDAV_PASSWORD"] == "pw-app"


def test_without_nextcloud_modules_no_nextcloud_questions(monkeypatch, tmp_path):
    asked, it = script(monkeypatch, ["", "Mario", "", "123:abc", "", "", "k", "", "", "n", "7,8", "tutti"])
    v = S.run(tmp_path / ".env")
    assert next(it, None) is None and v["EDITH_PLUGINS"] == "finance,server" and v["WEB_ENABLED"] == "0"
    assert not any("Nextcloud" in a for a in asked) and "CALDAV_URL" not in v and v["EDITH_QUOTE_PACKS"] == ""   # tutte le passioni = nessun filtro
    assert v["CHARTS_ENABLED"] == "0"                                                   # niente Nextcloud: grafici notturni spenti


def test_news_alone_asks_for_nextcloud_and_reminds_the_app(monkeypatch, tmp_path, capsys):
    asked, it = script(monkeypatch, ["", "Mario", "", "123:abc", "", "", "k", "", "", "n", "6", "cloud.x", "u", "p", "tutti"])
    v = S.run(tmp_path / ".env")
    assert next(it, None) is None and v["EDITH_PLUGINS"] == "news" and v["CALDAV_URL"] == "https://cloud.x/remote.php/dav"
    out = capsys.readouterr().out
    assert "app News" in out and "servono le app News" in out and "CHARTS_ENABLED" not in v


def test_rerun_proposes_previous_answers_and_keeps_unknown_settings(monkeypatch, tmp_path):
    p = tmp_path / ".env"
    p.write_text("OWNER_NAME=Mario\nTELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID=4242\nGEMINI_API_KEY=k\nWEB_ENABLED=0\nEDITH_PLUGINS=none\nNEWS_TIME=13:00\n")
    asked, it = script(monkeypatch, [""] * 9 + ["nessuno", "nessuno"])
    v = S.run(p)
    assert v["OWNER_NAME"] == "Mario" and v["TELEGRAM_BOT_TOKEN"] == "123:abc" and v["EDITH_PLUGINS"] == "none" and v["NEWS_TIME"] == "13:00"
    assert v["EDITH_QUOTE_PACKS"] == "nessuno" and next(it, None) is None


def test_check_mode(monkeypatch, tmp_path, capsys):
    p = tmp_path / ".env"
    assert S.check_only(p) == 1
    p.write_text("TELEGRAM_BOT_TOKEN=t\nGEMINI_API_KEY=k\nWEATHER_PLACE=Trieste\n")
    monkeypatch.setattr(S, "check_telegram", lambda t: "@EdithBot")
    monkeypatch.setattr(S, "check_gemini", lambda k, m: "risponde")
    monkeypatch.setattr(S, "geocode", lambda c: ("Trieste", 1, 2, "IT"))
    assert S.check_only(p) == 0 and "Tutto a posto" in capsys.readouterr().out

    def bad(*a):
        raise S.CheckError("no")
    monkeypatch.setattr(S, "check_gemini", bad)
    assert S.check_only(p) == 1
