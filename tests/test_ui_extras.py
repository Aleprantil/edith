"""Frasi, meteo a tre giorni con alba e tramonto, orari degli eventi, e coerenza tra pagina e codice."""
import json
import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from services import weather_service as W
from test_web_app import make
from web import cards

STATIC = Path(__file__).resolve().parent.parent / "web" / "static"
TZ = ZoneInfo("Europe/Rome")
APP_JS = (STATIC / "app.js").read_text(encoding="utf-8")
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")
POOL = json.loads((STATIC / "frasi.json").read_text(encoding="utf-8"))["frasi"]

FLAGS_IN_JS = set(re.findall(r"\bf\.(\w+)\s*=", APP_JS[APP_JS.index("function flags("):APP_JS.index("function pickQuote(")]))


# ---------------------------------------------------------------------------
# Frasi
# ---------------------------------------------------------------------------

def test_the_pool_is_valid_big_and_without_duplicates():
    assert len(POOL) >= 120
    texts = [f["t"] for f in POOL]
    assert len({t.lower() for t in texts}) == len(texts)
    assert all(isinstance(t, str) and 12 <= len(t) <= 100 for t in texts), [t for t in texts if not 12 <= len(t) <= 100]
    assert not any(re.search(r"[<>]|https?:|\\n", t) for t in texts)                      # solo testo semplice
    commands = ("sudo ", "rm -rf ")                                                         # frasi che cominciano con il nome di un comando
    assert all(t == t.strip() and (t[0].isupper() or t.startswith(commands)) for t in texts), [x for x in texts if not (x[0].isupper() or x.startswith(commands))]


def test_every_condition_in_the_pool_is_one_the_page_can_actually_evaluate():
    used = {k for f in POOL for k in f.get("c", [])}
    assert used and used <= FLAGS_IN_JS, used - FLAGS_IN_JS
    assert all(isinstance(f.get("c", []), list) and all(isinstance(k, str) for k in f.get("c", [])) for f in POOL)


def test_every_context_the_page_computes_has_phrases_and_there_are_plenty_of_general_ones():
    by_flag = {k: 0 for k in FLAGS_IN_JS}
    for f in POOL:
        for k in f.get("c", []):
            by_flag[k] += 1
    missing = [k for k, n in by_flag.items() if n == 0 and k not in {"martedi"}]
    assert not missing, missing                                                          # nessun contesto calcolato dalla pagina resta senza frasi
    assert sum(1 for f in POOL if not f.get("c")) >= 60                                  # abbastanza frasi sempre valide da non ripetersi


@pytest.mark.parametrize("topic,pattern", [("calcio", r"calcio|mister|allenament|fuorigioco|pallone"), ("sci e neve", r"\bsci|neve|piste|sciolina|discesa"),
                                           ("Linux", r"Linux|kernel|sudo|terminale|vim|pacman"), ("università", r"esame|università|lezion|sistemi operativi|database"),
                                           ("server", r"server|backup|Nextcloud|Tailscale|disco"), ("robotica e stampa 3D", r"robot|3D|ESP32|firmware|filamento"),
                                           ("Marvel", r"multiverso|Gemma|Wakanda|Vibranio|Hulk|Thor|Vendicatori|Avengers|JARVIS|Groot")])
def test_the_pool_covers_your_passions(topic, pattern):
    assert sum(1 for f in POOL if re.search(pattern, f["t"], re.I)) >= 5, topic


def test_the_pool_does_not_embed_well_known_film_lines():
    """Frasi originali: niente battute celebri (controllo su un piccolo elenco di quelle più ovvie)."""
    blob = " ".join(f["t"].lower() for f in POOL)
    for famous in ("i am iron man", "io sono iron man", "i am groot", "io sono groot", "avengers, assemble", "vendicatori, uniti", "with great power", "grandi poteri",
                   "i am inevitable", "sono inevitabile", "i love you 3000", "ti voglio bene 3000", "wakanda forever"):
        assert famous not in blob, famous


def test_the_pool_is_served_to_the_page():
    c, *_ = make()
    r = c.get("/static/frasi.json")
    assert r.status_code == 200 and "json" in r.headers["content-type"] and "no-cache" in r.headers["cache-control"]
    assert r.json()["frasi"] == POOL


# ---------------------------------------------------------------------------
# Pagina e codice vanno d'accordo
# ---------------------------------------------------------------------------

def test_every_element_the_code_looks_for_exists_in_the_page():
    ids = set(re.findall(r"\$\('#([\w-]+)'\)", APP_JS))
    assert {"quote", "next", "wx3", "sunline", "quick", "panel", "pscrim", "btn-panel", "btn-desk", "desk-exit", "dimmer"} <= ids
    assert not [i for i in sorted(ids) if f'id="{i}"' not in INDEX], [i for i in sorted(ids) if f'id="{i}"' not in INDEX]
    assert "wxline" not in APP_JS and "wxline" not in INDEX


def test_the_page_has_no_inline_script_or_style_and_stays_one_script():
    assert INDEX.count("<script") == 1 and "style=" not in INDEX and " onclick=" not in INDEX


def test_saved_settings_are_validated_before_use():
    """normalizeUI scarta tutto ciò che non conosce: un valore sbagliato nel telefono non può rompere la pagina."""
    body = APP_JS[APP_JS.index("function normalizeUI"):APP_JS.index("function loadUI")]
    for needle in ("Array.isArray", "ok.includes(id)", ".slice(0, 4)", "v.quotes !== false"):
        assert needle in body, needle


# ---------------------------------------------------------------------------
# Meteo a tre giorni, alba e tramonto
# ---------------------------------------------------------------------------

class FakeResp:
    status_code = 200

    def __init__(self, daily):
        self._daily = daily

    def json(self):
        return {"daily": self._daily}


def forecast(monkeypatch, daily):
    seen = {}

    def fake_get(url, timeout=None, params=None):
        seen["params"] = params
        return FakeResp(daily)
    monkeypatch.setattr(W._session, "get", fake_get)
    monkeypatch.setattr(W, "WEATHER_ENABLED", True)
    W._cache.update(ts=0.0, data=None)
    return seen


DAILY = {"time": ["2026-10-07", "2026-10-08", "2026-10-09"], "weather_code": [2, 63, 0], "temperature_2m_max": [19.2, 14.1, 16.6], "temperature_2m_min": [9.4, 8.0, 7.2],
         "precipitation_probability_max": [20, 70, 10], "precipitation_sum": [0, 6.2, 0], "sunrise": ["2026-10-07T07:12", "2026-10-08T07:13", "2026-10-09T07:15"],
         "sunset": ["2026-10-07T18:03", "2026-10-08T18:01", "2026-10-09T17:59"]}


def test_forecast_asks_for_three_days_with_sunrise_and_sunset(monkeypatch):
    seen = forecast(monkeypatch, DAILY)
    days = W.get_forecast(force=True)
    assert seen["params"]["forecast_days"] == 3 and "sunrise" in seen["params"]["daily"] and "sunset" in seen["params"]["daily"]
    assert [d["date"] for d in days] == DAILY["time"] and days[0]["sunrise"] == "07:12" and days[2]["sunset"] == "17:59"
    assert (days[0]["tmin"], days[0]["tmax"], days[1]["emoji"]) == (9, 19, "🌧️")


def test_forecast_survives_a_response_without_sun_times(monkeypatch):
    daily = {k: v for k, v in DAILY.items() if k not in ("sunrise", "sunset")}
    forecast(monkeypatch, daily)
    days = W.get_forecast(force=True)
    assert len(days) == 3 and all(d["sunrise"] is None and d["sunset"] is None for d in days)


@pytest.mark.parametrize("raw,expected", [("2026-10-07T07:12", "07:12"), ("2026-10-07T18:03:00", "18:03"), (None, None), ("", None), ("boh", None), (123, None), ("2026-10-07", None)])
def test_hhmm(raw, expected):
    assert W._hhmm(raw) == expected


def test_card_today_offers_three_weather_days_and_the_sun(monkeypatch):
    from services import nextcloud_service as NC
    monkeypatch.setattr(NC, "get_calendar_events", lambda a, b: [])
    days = [{"date": "2026-10-06", "tmin": 9, "tmax": 19, "pop": 20, "label": "nuvoloso", "emoji": "⛅", "sunrise": "07:12", "sunset": "18:03"},
            {"date": "2026-10-07", "tmin": 8, "tmax": 14, "pop": 70, "label": "pioggia", "emoji": "🌧️", "sunrise": "07:13", "sunset": "18:01"},
            {"date": "2026-10-08", "tmin": 7, "tmax": 16, "pop": 10, "label": "sereno", "emoji": "☀️", "sunrise": "07:15", "sunset": "17:59"}]
    monkeypatch.setattr(cards.weather_service, "get_forecast", lambda force=False: days)
    d = cards.card_today(datetime(2026, 10, 6, 10, 0, tzinfo=TZ))
    assert [w["label"] for w in d["weather_days"]] == ["oggi", "domani", "gio"]                       # l'8 ottobre 2026 è un giovedì
    assert d["weather_days"][1] == {"label": "domani", "tmin": 8, "tmax": 14, "emoji": "🌧️", "desc": "pioggia", "pop": 70}
    assert d["sun"] == {"rise": "07:12", "set": "18:03"} and d["weather"]["tmax"] == 19 and d["weather_tomorrow"]["pop"] == 70
    json.dumps(d)
    no_sun = [{k: v for k, v in x.items() if k not in ("sunrise", "sunset")} for x in days]
    monkeypatch.setattr(cards.weather_service, "get_forecast", lambda force=False: no_sun)
    assert cards.card_today(datetime(2026, 10, 6, 10, 0, tzinfo=TZ))["sun"] is None
    monkeypatch.setattr(cards.weather_service, "get_forecast", lambda force=False: days[:1])
    assert [w["label"] for w in cards.card_today(datetime(2026, 10, 6, 10, 0, tzinfo=TZ))["weather_days"]] == ["oggi"]
    monkeypatch.setattr(cards.weather_service, "get_forecast", lambda force=False: [{**days[0], "date": "boh"}])
    assert cards.card_today(datetime(2026, 10, 6, 10, 0, tzinfo=TZ))["weather_days"][0]["label"] == "oggi"        # data illeggibile: non si rompe


# ---------------------------------------------------------------------------
# Orari esatti degli eventi (per il «prossimo impegno»)
# ---------------------------------------------------------------------------

def test_events_carry_exact_start_and_end_for_the_countdown():
    now = datetime(2026, 10, 6, 10, 0, tzinfo=TZ)
    timed = {"title": "Robotics", "start": datetime(2026, 10, 6, 13, 30, tzinfo=TZ), "end": datetime(2026, 10, 6, 16, 30, tzinfo=TZ), "calendar": "università", "location": "Aula A105"}
    no_end = {"title": "Dentista", "start": datetime(2026, 10, 6, 15, 0, tzinfo=TZ), "calendar": "svago"}
    allday = {"title": "Compleanno", "start": date(2026, 10, 6), "calendar": "svago"}
    a, b, c = (cards._event_json(e, now) for e in (timed, no_end, allday))
    assert a["start_iso"] == "2026-10-06T13:30:00+02:00" and a["end_iso"] == "2026-10-06T16:30:00+02:00"
    assert b["end_iso"] == "2026-10-06T16:00:00+02:00"                                       # senza fine: un'ora
    assert c["start_iso"] is None and c["end_iso"] is None and c["all_day"]
    assert datetime.fromisoformat(a["start_iso"]).utcoffset().total_seconds() == 7200        # la pagina li legge con il fuso giusto


def test_the_countdown_logic_is_in_the_page_and_handles_the_four_cases():
    body = APP_JS[APP_JS.index("function nextEvent"):APP_JS.index("function updateNext")]
    for needle in ("in corso", "inLabel(m)", "alle ${hm(nxt.s)}", "g.label === 'Domani'", "d.upcoming"):
        assert needle in body, needle
    assert "updateNext(); updateDimmer();" in APP_JS[APP_JS.index("function tickClock"):APP_JS.index("function setDateLine")]      # si aggiorna ogni minuto



# ---------------------------------------------------------------------------
# Pagina di prova della voce
# ---------------------------------------------------------------------------

def test_the_voice_test_page_is_served_and_follows_the_security_rules():
    c, *_ = make()
    page = c.get("/static/voce-test.html")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    html = page.text
    assert html.count("<script") == 1 and 'src="/static/voce-test.js"' in html and "style=" not in html and " onclick=" not in html
    assert "<style" not in html and 'rel="stylesheet" href="/static/style.css"' in html
    js = c.get("/static/voce-test.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    body = js.text
    for needle in ("new SR()", "it-IT", "continuous = false", "r.abort()", "speechSynthesis", "2° ascolto"):
        assert needle in body, needle
    for forbidden in ("fetch(", "XMLHttpRequest", "sendBeacon", "localStorage", "innerHTML", "eval("):
        assert forbidden not in body, forbidden                      # la prova non invia né salva nulla
    ids = set(re.findall(r"\$\('#([\w-]+)'\)", body))
    assert ids <= {"log", "env", "t1", "t2", "copy"} and all(f'id="{i}"' in html for i in ids)
