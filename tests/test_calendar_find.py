"""Le funzioni nuove di nextcloud_service (cerca → conferma → modifica) con eventi iCalendar veri e un calendario finto."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import vobject

from services import nextcloud_service as NC

TZ = ZoneInfo("Europe/Rome")
DAY = datetime(2026, 10, 5, 0, 0, tzinfo=TZ)


def ics(summary, start, end, rrule=None, loc=""):
    f = lambda d: d.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    body = [f"SUMMARY:{summary}", f"DTSTART:{f(start)}", f"DTEND:{f(end)}", "UID:" + summary.replace(" ", "-")]
    if rrule:
        body.append("RRULE:" + rrule)
    if loc:
        body.append("LOCATION:" + loc)
    return "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//t//\nBEGIN:VEVENT\n" + "\n".join(body) + "\nEND:VEVENT\nEND:VCALENDAR\n"


class FakeEvent:
    def __init__(self, text):
        self.vobject_instance = vobject.readOne(text)
        self.saved, self.deleted, self.fail = 0, False, None

    def save(self):
        if self.fail == "save":
            raise OSError("rete")
        self.saved += 1

    def delete(self):
        if self.fail == "delete":
            raise OSError("rete")
        self.deleted = True


def cal(name):
    return SimpleNamespace(name=name)


@pytest.fixture
def world(monkeypatch):
    dentist = FakeEvent(ics("Dentista", DAY.replace(hour=15), DAY.replace(hour=16), loc="Via Roma"))
    gym = FakeEvent(ics("Palestra", DAY.replace(hour=18), DAY.replace(hour=19, minute=30)))
    soccer = FakeEvent(ics("Calcio", DAY.replace(hour=20), DAY.replace(hour=21), rrule="FREQ=WEEKLY"))
    dentist2 = FakeEvent(ics("Dentista (controllo)", DAY.replace(hour=9), DAY.replace(hour=10)))
    pairs = [(cal("lavoro"), dentist2), (cal("svago"), dentist), (cal("svago"), gym), (cal("calcio"), soccer)]
    seen = {}
    monkeypatch.setattr(NC, "_search_all", lambda a, b: (seen.setdefault("range", (a, b)), pairs)[1])
    monkeypatch.setattr(NC, "invalidate_cache", lambda: seen.setdefault("invalidated", True))
    return SimpleNamespace(dentist=dentist, gym=gym, soccer=soccer, dentist2=dentist2, seen=seen)


def test_find_does_not_touch_anything(world):
    d, ev = NC.find_event("svago", DAY, "palestra")
    assert d["title"] == "Palestra" and ev is world.gym
    assert world.gym.saved == 0 and not world.gym.deleted


def test_find_prefers_the_named_calendar_then_the_earliest(world):
    d, ev = NC.find_event("svago", DAY, "dentista")
    assert ev is world.dentist and d["calendar"] == "svago" and d["location"] == "Via Roma"       # non quello di «lavoro» delle 9
    d, ev = NC.find_event("lavoro", DAY, "dentista")
    assert ev is world.dentist2
    d, ev = NC.find_event("generale", DAY, "dentista")                                           # calendario non indicato: vince l'orario
    assert ev is world.dentist2


def test_find_uses_the_requested_window_and_is_case_insensitive(world):
    NC.find_event("svago", DAY.replace(hour=13), "PALESTRA", days=14)
    a, b = world.seen["range"]
    assert a == DAY and b == DAY + timedelta(days=14)


def test_find_needs_a_title_and_returns_none_when_nothing_matches(world):
    assert NC.find_event("svago", DAY, "") is None and NC.find_event("svago", DAY, "   ") is None
    assert NC.find_event("svago", DAY, "inesistente") is None


def test_find_refuses_recurring_events_with_the_action_name(world):
    with pytest.raises(NC.CalendarError) as e:
        NC.find_event("calcio", DAY, "calcio", action="cancellare")
    assert "ricorrente" in str(e.value) and "cancellare" in str(e.value)


def test_delete_found_deletes_and_reports_failures(world):
    d, ev = NC.find_event("svago", DAY, "palestra")
    NC.delete_found(ev, d)
    assert world.gym.deleted
    world.gym.fail, world.gym.deleted = "delete", False
    with pytest.raises(NC.CalendarError):
        NC.delete_found(ev, d)
    assert world.seen.get("invalidated")                                                           # la cache si svuota se qualcosa va storto


def test_update_found_moves_keeping_duration_and_writes_utc(world):
    d, ev = NC.find_event("svago", DAY, "palestra")
    new = datetime(2026, 10, 6, 10, 0, tzinfo=TZ)
    NC.update_found(ev, d, new)
    v = world.gym.vobject_instance.vevent
    assert world.gym.saved == 1
    assert v.dtstart.value == datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc)                     # 10:00 ora legale (+2) = 08:00 UTC
    assert v.dtend.value - v.dtstart.value == timedelta(minutes=90)                               # la durata resta
    assert v.dtstart.value.utcoffset() == timedelta(0) and "TZID" not in v.dtstart.serialize()         # niente TZID fisso: il bug dell'ora legale


def test_update_found_with_explicit_end_and_naive_datetimes(world):
    d, ev = NC.find_event("svago", DAY, "palestra")
    NC.update_found(ev, d, datetime(2026, 12, 1, 9, 0), datetime(2026, 12, 1, 9, 45))             # senza fuso: Europe/Rome; d'inverno +1
    v = world.gym.vobject_instance.vevent
    assert v.dtstart.value == datetime(2026, 12, 1, 8, 0, tzinfo=timezone.utc) and v.dtend.value == datetime(2026, 12, 1, 8, 45, tzinfo=timezone.utc)


def test_update_found_failure_is_reported(world):
    d, ev = NC.find_event("svago", DAY, "palestra")
    world.gym.fail = "save"
    with pytest.raises(NC.CalendarError):
        NC.update_found(ev, d, datetime(2026, 10, 6, 10, 0, tzinfo=TZ))
    assert world.seen.get("invalidated")
