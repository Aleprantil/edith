"""Dati delle schede: calendario, note, finanze (con un vero database SQLite), news, server."""
import json
import sys
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from services import monitor_service, news_service, weather_service
from services import nextcloud_service as NC
from services import notes_service as NS
from web import cards

TZ = ZoneInfo("Europe/Rome")
NOW = datetime(2026, 10, 5, 10, 30, tzinfo=TZ)


def ev(title, day, h, m=0, dur=60, cal="svago", loc=""):
    s = datetime(2026, 10, 5 + day, h, m, tzinfo=TZ)
    return {"title": title, "start": s, "end": s + timedelta(minutes=dur), "calendar": cal, "location": loc, "recurring": False}


FORECAST = [{"tmin": 9, "tmax": 19, "pop": 20, "label": "nuvoloso", "emoji": "⛅"}, {"tmin": 8, "tmax": 14, "pop": 70, "label": "pioggia", "emoji": "🌧️"}]


# ---------------------------------------------------------------------------
# Oggi
# ---------------------------------------------------------------------------

def test_today_flags_past_current_and_future_events_and_groups_the_next_days(monkeypatch):
    allday = {"title": "Compleanno Anna", "start": date(2026, 10, 5), "end": date(2026, 10, 6), "calendar": "svago", "location": "", "recurring": False}
    monkeypatch.setattr(NC, "get_calendar_events", lambda a, b: [ev("Lezione", 0, 9), ev("Call", 0, 10, dur=90), ev("Cena", 0, 20), allday,
                                                                  ev("Colazione", 1, 8), ev("Esame", 1, 14), ev("Dentista", 3, 9)])
    monkeypatch.setattr(weather_service, "get_forecast", lambda force=False: FORECAST)
    d = cards.card_today(NOW)
    by = {e["title"]: e for e in d["events"]}
    assert by["Lezione"]["past"] and not by["Lezione"]["now"]
    assert by["Call"]["now"] and not by["Call"]["past"]
    assert not by["Cena"]["past"] and not by["Cena"]["now"]
    assert by["Compleanno Anna"]["all_day"] and by["Compleanno Anna"]["time"] == ""
    assert d["greeting"] == "Buongiorno" and d["date_label"] == "lunedì 5 ottobre"
    assert [g["label"] for g in d["upcoming"]] == ["Domani", "giovedì 8 ottobre"] and len(d["upcoming"][0]["events"]) == 2
    assert d["weather"]["tmax"] == 19 and d["weather_tomorrow"]["pop"] == 70 and d["errors"] == []
    assert d["summary"] and all(set(w) == {"from", "to", "minutes"} for w in d["windows"])
    json.dumps(d)                                            # tutto serializzabile


def test_today_survives_a_dead_calendar_and_a_dead_weather_service(monkeypatch):
    def down(*a, **k):
        raise NC.CalendarError("Nextcloud non risponde")
    monkeypatch.setattr(NC, "get_calendar_events", down)
    monkeypatch.setattr(weather_service, "get_forecast", lambda force=False: FORECAST)
    d = cards.card_today(NOW)
    assert d["events"] == [] and d["weather"] and any("Nextcloud non risponde" in e for e in d["errors"])

    monkeypatch.setattr(NC, "get_calendar_events", lambda a, b: [])
    monkeypatch.setattr(weather_service, "get_forecast", lambda force=False: (_ for _ in ()).throw(RuntimeError("meteo giù")))
    d = cards.card_today(NOW)
    assert d["weather"] is None and any("Meteo" in e for e in d["errors"]) and d["events"] == []


@pytest.mark.parametrize("hour,expected", [(3, "Buonanotte"), (9, "Buongiorno"), (15, "Buon pomeriggio"), (21, "Buonasera")])
def test_greeting_by_hour(hour, expected):
    assert cards.greeting(NOW.replace(hour=hour)) == expected


# ---------------------------------------------------------------------------
# Note
# ---------------------------------------------------------------------------

def test_notes_sorted_by_last_edit_with_previews_for_the_first_three(monkeypatch):
    notes = [{"title": f"n{i}", "modified": 1000 + i} for i in range(8)] + [{"title": "senza data", "modified": None}]
    monkeypatch.setattr(NS, "list_notes", lambda cat="": notes)
    fetched = []
    monkeypatch.setattr(NS, "get_note_content", lambda t, c="": (fetched.append(t), f"# {t}\n*Creata il 01/10/2026*\n\n- latte\n- uova")[1])
    d = cards.card_notes()
    assert [n["title"] for n in d["items"]][:3] == ["n7", "n6", "n5"] and d["items"][-1]["title"] != "senza data" and d["total"] == 9
    assert fetched == ["n7", "n6", "n5"]                                       # una richiesta in più solo per le prime tre
    assert d["items"][0]["preview"] == "latte · uova" and d["items"][4]["preview"] == ""


def test_notes_preview_failure_does_not_break_the_card(monkeypatch):
    monkeypatch.setattr(NS, "list_notes", lambda cat="": [{"title": "a", "modified": 1}])
    monkeypatch.setattr(NS, "get_note_content", lambda t, c="": (_ for _ in ()).throw(NS.NotesError("giù")))
    assert cards.card_notes()["items"][0]["preview"] == ""


def test_note_preview_cleaning():
    assert cards._note_preview("# Titolo\n*Creata il 01/10*\n\nuno\ndue") == "uno · due"
    assert cards._note_preview("x" * 400).endswith("…") and len(cards._note_preview("x" * 400)) == 140
    assert cards._note_preview("") == ""


def test_notes_service_reads_last_modified_from_a_real_propfind_response(monkeypatch):
    xml = """<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">
      <d:response><d:href>/remote.php/dav/files/u/Notes/</d:href><d:propstat><d:prop/></d:propstat></d:response>
      <d:response><d:href>/remote.php/dav/files/u/Notes/Spesa.md</d:href><d:propstat><d:prop><d:getlastmodified>Sat, 03 Oct 2026 14:05:00 GMT</d:getlastmodified></d:prop></d:propstat></d:response>
      <d:response><d:href>/remote.php/dav/files/u/Notes/Vecchia.txt</d:href><d:propstat><d:prop/></d:propstat></d:response>
      <d:response><d:href>/remote.php/dav/files/u/Notes/foto.jpg</d:href><d:propstat><d:prop/></d:propstat></d:response></d:multistatus>"""

    class Resp:
        status_code, text, content = 207, xml, xml.encode()
    monkeypatch.setattr(NS, "_req", lambda *a, **k: Resp())
    notes = NS._list_folder("/remote.php/dav/files/u", "Notes")
    by = {n["title"]: n for n in notes}
    assert set(by) == {"Spesa", "Vecchia"}                                     # la foto e la cartella non sono note
    assert by["Spesa"]["modified"] == datetime(2026, 10, 3, 14, 5, tzinfo=ZoneInfo("UTC")).timestamp()
    assert by["Vecchia"]["modified"] is None
    assert NS._parse_http_date("non è una data") is None and NS._parse_http_date(None) is None


# ---------------------------------------------------------------------------
# Finanze (database vero)
# ---------------------------------------------------------------------------

@pytest.fixture
def fin_db():
    import database as db
    db.init_db()
    uid = 777
    with db.get_conn() as conn:                      # partenza pulita: il database di prova resta tra un test e l'altro
        conn.execute("DELETE FROM transactions WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM budgets WHERE user_id=?", (uid,))
    today = date.today()
    first = today.replace(day=1)
    db.add_transaction(uid, "spesa", "ristoranti", 18.0, "sushi", f"{first} 12:00:00")
    db.add_transaction(uid, "spesa", "ristoranti", 30.0, "cena", f"{first} 20:00:00")
    db.add_transaction(uid, "spesa", "trasporti", 20.0, "benzina", f"{first} 08:00:00")
    db.add_transaction(uid, "entrata", "stipendio", 1000.0, "mese", f"{first} 09:00:00")
    db.add_transaction(uid, "spesa", "ristoranti", 999.0, "mese scorso", f"{first - timedelta(days=3)} 12:00:00")   # NON deve contare
    db.set_budget(uid, "ristoranti", 40.0)
    db.set_budget(uid, "trasporti", 100.0)
    cards.OWNER_ID = uid
    return uid


def test_finance_card_totals_budgets_and_last_movements(fin_db):
    d = cards.card_finance()
    assert d["income"] == 1000.0 and d["expense"] == 68.0 and d["balance"] == 932.0
    assert d["categories"][0]["total"] == 48.0 and round(d["categories"][0]["pct"], 1) == round(100 * 48 / 68, 1)
    b = {x["label"].split(" ", 1)[1]: x for x in d["budgets"]}
    assert b["Ristoranti"]["pct"] == 120.0 and b["Trasporti"]["pct"] == 20.0 and d["budgets"][0]["pct"] >= d["budgets"][-1]["pct"]
    assert len(d["last"]) == 4 and {t["type"] for t in d["last"]} == {"spesa", "entrata"}
    assert d["month_label"].endswith(str(date.today().year))
    json.dumps(d)


def test_finance_card_empty_month_and_other_user_isolated(fin_db):
    cards.OWNER_ID = 123456
    d = cards.card_finance()
    assert d["balance"] == 0 and d["categories"] == [] and d["last"] == [] and d["budgets"] == []


def test_finance_card_unavailable_without_the_financebot(monkeypatch):
    monkeypatch.setitem(sys.modules, "database", None)
    with pytest.raises(cards.CardUnavailable):
        cards.card_finance()


# ---------------------------------------------------------------------------
# News e server
# ---------------------------------------------------------------------------

def test_news_card_orders_groups_strips_unsafe_urls(monkeypatch):
    now = time.time()
    mk = lambda folder, url: {"folder": folder, "feed": "", "all_ids": [], "items": [{"id": "1", "title": "T", "line": f"Titolo {folder}", "url": url, "source": "Fonte", "pub": now - 7200}]}
    monkeypatch.setattr(news_service, "collect_digest", lambda *a, **k: [mk("cyber", "javascript:alert(1)"), mk("sport", "https://ok.it/a"), mk("mondo", "")])
    d = cards.card_news()
    assert [g["title"] for g in d["groups"]][0].endswith("Sport")
    by = {g["title"]: g["items"][0] for g in d["groups"]}
    urls = [i["url"] for i in by.values()]
    assert "https://ok.it/a" in urls and "" in urls and not any("javascript" in u for u in urls)
    assert all(i["age"] == "2 h fa" for i in by.values())


def test_server_card_sanitizes_alert_html(monkeypatch):
    class Mon:
        def card(self):
            return {"alerts": [{"level": "bad", "text": "💿 <b>/mnt/data</b> <script>alert(1)</script> non montato"}]}
    monkeypatch.setattr(monitor_service, "MONITOR", Mon())
    a = cards.card_server()["alerts"][0]
    assert "<script" not in a["html"] and "<b>/mnt/data</b>" in a["html"] and a["level"] == "bad"


def test_real_monitor_card_has_every_field_the_page_reads_and_is_json(monkeypatch):
    monkeypatch.setattr(monitor_service, "MONITOR", None)
    c = monitor_service.get_monitor().card()
    for key in ("host", "uptime_s", "cpu", "mem", "temp", "root", "mounts", "power", "net", "nextcloud", "docker", "kernel", "smart", "alerts", "probe"):
        assert key in c, key
    assert {"pct", "cores", "load"} <= set(c["cpu"]) and {"used", "total", "pct"} <= set(c["mem"]) and {"cpu", "peak", "fans", "disks"} <= set(c["temp"])
    json.dumps(c)


def test_card_registry_is_complete():
    assert set(cards.ORDER) == set(cards.CARDS) == {"today", "tasks", "finance", "notes", "news", "server"}


def test_finance_series_for_the_charts(fin_db):
    import database as db
    uid, today = fin_db, date.today()
    prev = cards._month_start(today, -1)
    db.add_transaction(uid, "spesa", "trasporti", 40.0, "mese scorso", f"{prev.replace(day=3)} 10:00:00")
    db.add_transaction(uid, "entrata", "stipendio", 900.0, "mese scorso", f"{prev.replace(day=2)} 10:00:00")
    s = cards.card_finance()["series"]
    assert len(s["months"]) == 6 and s["months"][-1]["ym"] == today.strftime("%Y-%m") and s["months"][-2]["ym"] == prev.strftime("%Y-%m")
    assert s["months"][-1]["expense"] == 68.0 and s["months"][-1]["income"] == 1000.0               # le spese del fixture di questo mese
    assert s["months"][-2]["income"] == 900.0 and s["months"][0]["expense"] == 0.0
    assert len(s["daily"]) == today.day and round(sum(s["daily"]), 2) == 68.0
    assert len(s["daily_prev"]) == (cards._month_start(today, 0) - prev).days and s["daily_prev"][2] == 40.0
    assert s["month_days"] in (28, 29, 30, 31) and all(len(m["label"]) == 3 for m in s["months"])
    json.dumps(s)


def test_month_start_crosses_year_boundaries():
    assert cards._month_start(date(2026, 1, 15), -1) == date(2025, 12, 1)
    assert cards._month_start(date(2026, 1, 15), -5) == date(2025, 8, 1)
    assert cards._month_start(date(2026, 12, 31), 1) == date(2027, 1, 1)


def test_finance_card_survives_missing_series(fin_db, monkeypatch):
    import database as db
    monkeypatch.setattr(db, "get_daily_totals", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("giù")))
    d = cards.card_finance()
    assert d["series"] is None and d["expense"] == 68.0                                           # la scheda funziona anche senza grafici
