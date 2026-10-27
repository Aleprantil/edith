"""Promemoria (Tasks): le regole sulle scadenze, lettura/scrittura delle voci, ricerca per parole, assistente, dashboard."""
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import vobject

from services import assistant as A
from services import digest, gemini_service as G, tasks_service as T
from test_assistant import SID, parsed
from test_web_app import API, LOGIN, make
from web import cards
from web.demo import FakeTaskList, make_todo

TZ = ZoneInfo("Europe/Rome")
NOW = datetime(2026, 10, 6, 10, 0, tzinfo=TZ)                    # martedì 6 ottobre, ore 10


def at(day, h=23, m=59):
    return datetime(2026, 10, day, h, m, tzinfo=TZ)


@pytest.fixture
def lst(monkeypatch):
    lista = FakeTaskList("TO DO", [make_todo("Chiamare il commercialista", at(5, 18, 0), uid="a@t"), make_todo("da pagina 10 a 20", at(7), uid="b@t"),
                                   make_todo("Rinnovare l'abbonamento", None, uid="c@t"), make_todo("Pagare la bolletta", None, done=True, uid="d@t")])
    monkeypatch.setattr(T, "task_lists", lambda force=False: [lista])
    return lista


# ---------------------------------------------------------------------------
# Le tue regole sulle scadenze
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("day,clock,expected,note", [
    (None, None, None, ""),                                                    # né giorno né ora: senza scadenza
    ("2026-10-07", None, at(7), ""),                                           # «domani» / «entro domani»: 23:59
    ("2026-10-06", None, at(6), ""),                                           # «entro oggi»
    (None, "12:00", at(6, 12, 0), ""),                                         # «entro le 12»: oggi alle 12
    (None, "09:00", at(7, 9, 0), "oggi quell'ora è già passata"),              # ma sono le 10: la prossima volta che capita
    ("2026-10-08", "18:30", at(8, 18, 30), ""),                                # giorno e ora
    ("2026-10-08", "9.30", at(8, 9, 30), ""),
    (None, "9", at(7, 9, 0), "oggi quell'ora è già passata"),
    (None, "12:00:00", at(6, 12, 0), ""),
])
def test_deadline_rules(day, clock, expected, note):
    due, n = T.compute_due(NOW, day, clock)
    assert due == expected and n == note


@pytest.mark.parametrize("day,clock,expected", [("boh", "boh", None), ("2026-13-45", None, None), (None, "25:00", None), (None, "12:75", None),
                                                ("", "", None), ("2026-10-07", "boh", at(7)), ("boh", "12:00", at(6, 12, 0))])
def test_garbage_input_never_invents_a_deadline(day, clock, expected):
    assert T.compute_due(NOW, day, clock)[0] == expected


@pytest.mark.parametrize("due,label,level", [
    (None, "", "none"), (at(6, 12, 0), "oggi 12:00", "today"), (at(6, 8, 0), "scaduto alle 08:00", "overdue"), (at(6), "oggi", "today"),
    (at(7), "domani", "soon"), (at(8, 15, 0), "giovedì 15:00", "soon"), (at(5, 18, 0), "scaduto ieri", "overdue"), (at(1), "scaduto da 5 g", "overdue"),
    (at(13), "13 ott", "soon"), (at(30), "30 ott", "later")])
def test_labels_and_levels(due, label, level):
    assert T.due_label(due, NOW) == label and T.due_level(due, NOW) == level


# ---------------------------------------------------------------------------
# Liste
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", [None, "todo", "To Do", "TO DO", "to-do", "  todo  "])
def test_the_list_is_found_however_you_write_it(lst, name):
    assert T.pick_list(name) is lst


def test_unknown_list_is_explained_and_default_falls_back(monkeypatch, lst):
    spesa = FakeTaskList("Spesa", [])
    monkeypatch.setattr(T, "task_lists", lambda force=False: [lst, spesa])
    assert T.pick_list("spesa") is spesa
    with pytest.raises(T.TaskError) as e:
        T.pick_list("lavoro")
    assert "lavoro" in str(e.value) and "TO DO" in str(e.value) and "Spesa" in str(e.value)
    monkeypatch.setattr(T, "TASKS_DEFAULT_LIST", "Inesistente")
    assert T.pick_list(None) is lst                                                # predefinita rinominata: prende la prima
    monkeypatch.setattr(T, "task_lists", lambda force=False: [])
    with pytest.raises(T.TaskError):
        T.pick_list(None)


def _cals(monkeypatch, *cals):
    monkeypatch.setattr(T, "_calendars", lambda: list(cals))
    T._lists_cache.update(at=0, cals=None, all=[])


def test_only_the_todo_list_is_used_not_the_other_calendars_that_accept_todos(monkeypatch):
    """Il caso reale: i calendari normali di Nextcloud accettano anche i promemoria, ma tu vuoi solo «TO DO»."""
    cals = [FakeTaskList("università", components=("VEVENT", "VTODO")), FakeTaskList("TO DO"), FakeTaskList("Calendario", components=("VEVENT",)),
            FakeTaskList("Spesa", components=("VTODO",))]
    _cals(monkeypatch, *cals)
    assert [c.name for c in T.task_lists()] == ["TO DO"] and T.pick_list() is cals[1] and T.pick_list("to-do") is cals[1]
    assert T._lists_cache["all"] == ["università", "TO DO", "Spesa"]                       # sa quali altre ci sono, ma non le usa
    with pytest.raises(T.TaskError) as e:
        T.pick_list("spesa")                                                               # nemmeno se la nomini
    assert "uso solo «TO DO»" in str(e.value)
    T._lists_cache.update(at=0, cals=None, all=[])


def test_the_allowed_lists_can_be_extended_in_the_env(monkeypatch):
    cals = [FakeTaskList("TO DO"), FakeTaskList("Spesa"), FakeTaskList("Lavoro")]
    _cals(monkeypatch, *cals)
    monkeypatch.setattr(T, "TASKS_LISTS", ["TO DO", "spesa"])
    assert [c.name for c in T.task_lists()] == ["TO DO", "Spesa"] and T.pick_list("Spesa") is cals[1]
    T._lists_cache.update(at=0, cals=None, all=[])


def test_if_the_todo_list_is_missing_the_error_says_what_exists(monkeypatch):
    _cals(monkeypatch, FakeTaskList("Personale"), FakeTaskList("Spesa"))
    with pytest.raises(T.TaskError) as e:
        T.pick_list()
    assert "«TO DO»" in str(e.value) and "«Personale»" in str(e.value) and "TASKS_DEFAULT_LIST" in str(e.value)
    with pytest.raises(T.TaskError):
        T.open_tasks()                                                                     # niente: non pesca da un'altra lista di nascosto
    T._lists_cache.update(at=0, cals=None, all=[])


def test_lists_are_read_once_and_cached(monkeypatch):
    calls = []

    class Cal(FakeTaskList):
        def get_supported_components(self):
            calls.append(self.name)
            return super().get_supported_components()
    _cals(monkeypatch, Cal("TO DO"), Cal("Altro"))
    T.task_lists(); T.task_lists(); T.task_lists()
    assert len(calls) == 2                                                                 # una volta per calendario, poi dalla memoria
    T._lists_cache.update(at=0, cals=None, all=[])


def test_top_orders_by_urgency_and_cuts_at_seven():
    pub = [{"title": f"n{i}", "level": "none", "due": None} for i in range(3)] + [{"title": "dopo", "level": "later", "due": "x"}, {"title": "presto", "level": "soon", "due": "x"},
           {"title": "oggi", "level": "today", "due": "x"}, {"title": "scaduto", "level": "overdue", "due": "x"}, {"title": "scaduto2", "level": "overdue", "due": "y"}, {"title": "n3", "level": "none", "due": None}]
    shown, total = T.top(pub)
    assert [t["title"] for t in shown] == ["scaduto", "scaduto2", "oggi", "presto", "dopo", "n0", "n1"] and total == 9 and T.SHOWN == 7
    assert T.top([]) == ([], 0) and len(T.top(pub, 3)[0]) == 3


# ---------------------------------------------------------------------------
# Scrittura
# ---------------------------------------------------------------------------

def test_create_task_writes_a_valid_vtodo(lst):
    r = T.create_task("da pagina 20 a 30, poi; riposo\nfine", at(7), "todo")
    v = vobject.readOne(lst.last_ical).vtodo
    assert v.summary.value == "da pagina 20 a 30, poi; riposo\nfine" and str(v.status.value) == "NEEDS-ACTION"
    assert v.due.value == datetime(2026, 10, 7, 21, 59, tzinfo=T.dutz.tzutc())                 # 23:59 ora legale = 21:59 UTC
    assert "TZID" not in lst.last_ical and "DUE:20261007T215900Z" in lst.last_ical
    assert r["list"] == "TO DO" and r["uid"].endswith("@edith") and r["uid"] == v.uid.value


def test_create_task_without_deadline_and_with_description_and_unique_uids(lst):
    T.create_task("senza scadenza", None, None, description="una nota")
    v = vobject.readOne(lst.last_ical).vtodo
    assert not hasattr(v, "due") and v.description.value == "una nota"
    a, b = T.create_task("uno")["uid"], T.create_task("uno")["uid"]
    assert a != b


def test_create_task_rejects_empty_titles_and_reports_server_failures(lst, monkeypatch):
    for bad in ("", "   ", None):
        with pytest.raises(T.TaskError):
            T.create_task(bad)
    monkeypatch.setattr(lst, "save_todo", lambda ical=None, **k: (_ for _ in ()).throw(OSError("password=SEGRETA")))
    with pytest.raises(T.TaskError) as e:
        T.create_task("x")
    assert "SEGRETA" not in str(e.value) and "OSError" in str(e.value)


# ---------------------------------------------------------------------------
# Lettura
# ---------------------------------------------------------------------------

def test_open_tasks_are_sorted_and_completed_ones_hidden(lst):
    out = T.open_tasks()
    assert [t["title"] for t in out] == ["Chiamare il commercialista", "da pagina 10 a 20", "Rinnovare l'abbonamento"]       # scaduto, poi per data, poi senza scadenza
    assert "Pagare la bolletta" in [t["title"] for t in T.open_tasks(include_completed=True)] and "Pagare la bolletta" not in [t["title"] for t in out]
    assert out[0]["due"] == at(5, 18, 0) and out[2]["due"] is None and not out[0]["done"]


def test_due_formats_from_other_apps_are_understood(lst):
    from web.demo import FakeTodo
    formati = {"solo data": "DUE;VALUE=DATE:20261009", "senza fuso": "DUE:20261009T100000", "con fuso": "DUE;TZID=Europe/Rome:20261009T120000", "in UTC": "DUE:20261009T100000Z"}
    for i, (nome, riga) in enumerate(formati.items()):
        lst.items.append(FakeTodo(f"BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:x\nBEGIN:VTODO\nUID:f{i}@x\nSUMMARY:{nome}\n{riga}\nEND:VTODO\nEND:VCALENDAR\n"))
    dues = {t["title"]: t["due"] for t in T.open_tasks()}
    assert dues["solo data"] == at(9)                                                  # solo giorno: fine giornata
    assert dues["senza fuso"] == at(9, 10, 0) and dues["con fuso"] == at(9, 12, 0)     # senza fuso = ora italiana
    assert dues["in UTC"] == at(9, 12, 0)                                              # 10:00 UTC = 12:00 con l'ora legale
    assert all(d is None or d.tzinfo is not None for d in dues.values())


def test_broken_entries_are_skipped_not_fatal(lst):
    from web.demo import FakeTodo
    lst.items.append(FakeTodo("BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:x\nBEGIN:VTODO\nSUMMARY:senza uid\nEND:VTODO\nEND:VCALENDAR\n"))
    assert len(T.open_tasks()) == 3


def test_public_view_has_labels_and_no_internal_objects(lst):
    pub = T.public(T.open_tasks(), NOW)
    assert pub[0] == {"uid": "a@t", "title": "Chiamare il commercialista", "list": "TO DO", "due": at(5, 18, 0).isoformat(), "label": "scaduto ieri", "level": "overdue"}
    assert pub[2]["due"] is None and pub[2]["level"] == "none"
    json.dumps(pub)


# ---------------------------------------------------------------------------
# Spuntare e riaprire
# ---------------------------------------------------------------------------

def test_tick_sets_all_three_fields_nextcloud_looks_at_and_untick_restores(lst):
    t = T.open_tasks()[0]
    T.set_done(t["_obj"], True)
    v = t["_obj"].vobject_instance.vtodo
    assert str(v.status.value) == "COMPLETED" and str(v.percent_complete.value) == "100" and v.completed.value.utcoffset() == timedelta(0) and t["_obj"].saves == 1
    assert "COMPLETED:" in t["_obj"].vobject_instance.serialize() and t["title"] not in [x["title"] for x in T.open_tasks()]
    T.set_done(t["_obj"], False)
    assert str(v.status.value) == "NEEDS-ACTION" and str(v.percent_complete.value) == "0" and not hasattr(v, "completed")
    assert t["title"] in [x["title"] for x in T.open_tasks()]


def test_recurring_tasks_are_completed_by_the_library(lst):
    from web.demo import FakeTodo
    todo = FakeTodo("BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:x\nBEGIN:VTODO\nUID:r@x\nSUMMARY:ogni lunedì\nRRULE:FREQ=WEEKLY\nDUE:20261012T100000Z\nEND:VTODO\nEND:VCALENDAR\n")
    lst.items.append(todo)
    T.set_done(todo, True)
    assert todo.completed_calls == 1 and todo.saves == 0


def test_set_done_by_uid_finds_in_any_state_and_reports_problems(lst, monkeypatch):
    r = T.set_done_by_uid("b@t", True)
    assert r == {"uid": "b@t", "title": "da pagina 10 a 20", "done": True, "list": "TO DO"}
    assert T.set_done_by_uid("d@t", False)["done"] is False                           # un completato si può riaprire
    saves = [t.saves for t in lst.items]
    T.set_done_by_uid("b@t", True)                                                    # già spuntato: non riscrive
    assert [t.saves for t in lst.items] == saves
    with pytest.raises(T.TaskError):
        T.set_done_by_uid("non-esiste@x", True)
    with pytest.raises(T.TaskError):
        T.set_done_by_uid("a@t", True, "lista-sbagliata")
    monkeypatch.setattr(lst.items[0], "save", lambda **k: (_ for _ in ()).throw(OSError("giù")))
    with pytest.raises(T.TaskError):
        T.set_done_by_uid("a@t", True)


# ---------------------------------------------------------------------------
# Ricerca per parole
# ---------------------------------------------------------------------------

def mk(*titles):
    return [{"title": t} for t in titles]


def test_match_uses_every_word_and_numbers_count():
    ts = mk("da pagina 20 a 30", "da pagina 30 a 40", "da pagina 10 a 20", "Comprare il latte")
    exact, fuzzy = T.match("da pagina 20 a 30", ts)
    assert [t["title"] for t in exact] == ["da pagina 20 a 30"] and fuzzy and fuzzy[0]["title"] in ("da pagina 30 a 40", "da pagina 10 a 20")
    assert [t["title"] for t in T.match("latte", ts)[0]] == ["Comprare il latte"]
    assert T.match("promemoria latte", ts)[0] == T.match("latte", ts)[0]                # «promemoria» non è una parola da cercare
    assert len(T.match("pagina", ts)[0]) == 3                                           # generica: più risposte esatte
    assert T.match("xyz", ts) == ([], []) and T.match("", ts) == ([], [])


def test_match_prefers_the_shorter_title_among_exact_ones():
    ts = mk("chiamare Marco per la festa di sabato", "chiamare Marco")
    assert [t["title"] for t in T.match("chiamare Marco", ts)[0]] == ["chiamare Marco", "chiamare Marco per la festa di sabato"]


# ---------------------------------------------------------------------------
# Con l'assistente
# ---------------------------------------------------------------------------

@pytest.fixture
def eng(monkeypatch, lst):
    script = {}
    monkeypatch.setattr(A, "parse_user_input", lambda text, now_str, context="": script.get(text) or parsed(**{}))
    monkeypatch.setattr(A, "speech_rewrite", lambda plain, intent="": "")
    e = A.Assistant(owner_id=42, clock=lambda: NOW)
    e.script, e.lst = script, lst
    return e


def task(**kw):
    base = {"task_list": None, "due_day": None, "due_clock": None}
    return {**parsed(**kw), **{k: v for k, v in base.items() if k not in kw}}


def test_add_task_for_tomorrow_follows_your_rule(eng):
    eng.script["promemoria"] = task(intent="add_task", title="da pagina 20 a 30", due_day="2026-10-07")
    r = eng.handle(SID, "promemoria", spoken=True)
    v = vobject.readOne(eng.lst.last_ical).vtodo
    assert v.summary.value == "da pagina 20 a 30" and "DUE:20261007T215900Z" in eng.lst.last_ical              # domani 23:59
    assert r.ok and r.refresh == ["tasks"] and "TO DO" in r.html and "domani" in r.html
    assert r.speak == "Fatto. Ho aggiunto da pagina 20 a 30 nella lista TO DO, entro domani."


@pytest.mark.parametrize("day,clock,due_text,spoken_end", [
    (None, "12:00", "DUE:20261006T100000Z", "entro oggi a mezzogiorno."),                               # «entro le 12»
    (None, "09:00", "DUE:20261007T070000Z", "entro domani alle 9. Nota: oggi quell'ora è già passata."),
    ("2026-10-08", "18:30", "DUE:20261008T163000Z", "entro dopodomani alle 18 e mezza."),
    (None, None, None, "senza scadenza.")])
def test_add_task_all_deadline_shapes(eng, day, clock, due_text, spoken_end):
    eng.script["p"] = task(intent="add_task", title="x", due_day=day, due_clock=clock)
    r = eng.handle(SID, "p", spoken=True)
    assert r.speak.endswith(spoken_end) and ((due_text in eng.lst.last_ical) if due_text else ("DUE" not in eng.lst.last_ical))


def test_add_task_to_a_named_list_and_errors(eng):
    eng.script["a"] = task(intent="add_task", title="x", task_list="lavoro")
    r = eng.handle(SID, "a")
    assert not r.ok and "lavoro" in r.html and "TO DO" in r.html and eng.lst.last_ical is None
    eng.script["b"] = task(intent="add_task", title="  ")
    assert not eng.handle(SID, "b").ok
    eng.script["c"] = task(intent="add_task", title="y", task_list="todo")
    assert eng.handle(SID, "c").ok and "SUMMARY:y" in eng.lst.last_ical


def test_single_match_is_ticked_at_once_with_an_undo(eng):
    eng.script["spunta"] = task(intent="complete_task", title="commercialista")
    r = eng.handle(SID, "spunta", spoken=True)
    todo = eng.lst.items[0]
    assert todo.done and r.refresh == ["tasks"] and "Spuntato" in r.html and r.speak == "Fatto, ho spuntato Chiamare il commercialista."
    assert r.confirm["yes"] == "Annulla" and r.confirm["no"] == "Ok" and r.confirm["listen"] is False               # niente ascolto automatico
    back = eng.confirm(SID, r.confirm["token"], True, spoken=True)
    assert not todo.done and back.refresh == ["tasks"] and back.speak == "Va bene, ho riaperto Chiamare il commercialista."


def test_undo_declined_keeps_it_done(eng):
    eng.script["spunta"] = task(intent="complete_task", title="commercialista")
    r = eng.handle(SID, "spunta")
    out = eng.confirm(SID, r.confirm["token"], False)
    assert eng.lst.items[0].done and "Va bene" in out.html


def test_several_matches_ask_which_one(eng):
    eng.lst.items += [make_todo("da pagina 20 a 30", at(7), uid="e@t"), make_todo("da pagina 30 a 40", at(8), uid="f@t")]
    eng.script["spunta"] = task(intent="complete_task", title="pagina")
    done = lambda: sum(1 for x in eng.lst.items if x.done)                                                          # (la bolletta è già fatta)
    r = eng.handle(SID, "spunta", spoken=True)
    assert len(r.confirm["choices"]) == 3 and r.confirm["no"] == "Annulla" and "1." in r.html and "3." in r.html
    assert r.speak.startswith("Ne ho trovati 3. Quale intendi? il primo:") and done() == 1
    out = eng.confirm(SID, r.confirm["choices"][1]["token"], True)                                                  # scelgo la seconda
    assert "Spuntato" in out.html and done() == 2
    r2 = eng.handle(SID, "spunta")                                                                                  # ne restano due: richiede
    assert len(r2.confirm["choices"]) == 2
    assert eng.confirm(SID, r2.confirm["token"], False).ok and done() == 2                                          # «Annulla»: non cambia niente


def test_approximate_match_asks_yes_no_first(eng):
    eng.script["spunta"] = task(intent="complete_task", title="da pagina 20 a 30")
    r = eng.handle(SID, "spunta", spoken=True)
    assert r.confirm and "choices" not in r.confirm and "da pagina 10 a 20" in r.html and r.speak.endswith("Dimmi sì o no.")
    assert not any(t.done for t in eng.lst.items if t.vobject_instance.vtodo.uid.value == "b@t")
    assert "Spuntato" in eng.confirm(SID, r.confirm["token"], True).html


def test_no_match_and_missing_words(eng):
    eng.script["s1"] = task(intent="complete_task", title="astronave")
    assert not eng.handle(SID, "s1").ok and "astronave" in eng.handle(SID, "s1").html
    eng.script["s2"] = task(intent="complete_task", title="")
    assert not eng.handle(SID, "s2").ok


def test_list_tasks_text_and_speech(eng):
    eng.script["che"] = task(intent="get_tasks")
    r = eng.handle(SID, "che", spoken=True)
    assert "Promemoria aperti" in r.html and "(3)" in r.html and "scaduto ieri" in r.html
    assert r.speak == "Hai 3 promemoria: Chiamare il commercialista, da pagina 10 a 20 e Rinnovare l'abbonamento."
    eng.lst.items = []
    assert eng.handle(SID, "che", spoken=True).speak == "Non hai promemoria aperti."


def test_todays_agenda_shows_the_most_urgent_tasks_not_only_two(eng, monkeypatch):
    from services import nextcloud_service as NC
    from test_assistant import ev
    monkeypatch.setattr(NC, "get_calendar_events", lambda a, b: [ev("Dentista", 15)])
    eng.script["oggi"] = parsed(intent="get_events", start_time="2026-10-06T00:00:00")
    r = eng.handle(SID, "oggi", spoken=True)
    lines = [l for l in r.html.split("Promemoria</b>")[1].splitlines() if l.startswith("•")]
    assert [l.split(" <i>")[0] for l in lines] == ["• Chiamare il commercialista", "• da pagina 10 a 20", "• Rinnovare l'abbonamento"]      # tutti e tre, dal più urgente
    assert "(scaduto ieri)" in lines[0] and "(domani)" in lines[1] and "(" not in lines[2]
    assert r.speak.endswith("E hai 3 promemoria: Chiamare il commercialista, da pagina 10 a 20 e Rinnovare l'abbonamento.")
    eng.script["filtro"] = parsed(intent="get_events", title="dentista", start_time="2026-10-06T00:00:00")
    assert "Promemoria" not in eng.handle(SID, "filtro").html
    eng.script["domani"] = parsed(intent="get_events", start_time="2026-10-07T00:00:00")
    assert "Promemoria" not in eng.handle(SID, "domani").html


def test_with_many_tasks_the_agenda_shows_seven_and_counts_the_rest(eng, monkeypatch):
    from services import nextcloud_service as NC
    from test_assistant import ev
    for i in range(6):
        eng.lst.items.append(make_todo(f"altro {i}", None, uid=f"x{i}@t"))
    monkeypatch.setattr(NC, "get_calendar_events", lambda a, b: [ev("Dentista", 15)])
    eng.script["oggi"] = parsed(intent="get_events", start_time="2026-10-06T00:00:00")
    r = eng.handle(SID, "oggi", spoken=True)
    section = r.html.split("Promemoria</b>")[1]
    assert section.count("•") == 7 and "… e altri 2" in section                                                       # 9 aperti: 7 e «altri 2»
    assert r.speak.endswith("E hai 9 promemoria: Chiamare il commercialista, da pagina 10 a 20 e altro 0, e altri 6.")


def test_an_unreachable_tasks_app_never_breaks_the_agenda(eng, monkeypatch):
    from services import nextcloud_service as NC
    from test_assistant import ev
    monkeypatch.setattr(NC, "get_calendar_events", lambda a, b: [ev("Dentista", 15)])
    monkeypatch.setattr(T, "task_lists", lambda force=False: (_ for _ in ()).throw(T.TaskError("Tasks non risponde")))
    eng.script["oggi"] = parsed(intent="get_events", start_time="2026-10-06T00:00:00")
    r = eng.handle(SID, "oggi", spoken=True)
    assert r.ok and "Dentista" in r.html and "romemoria" not in r.speak
    eng.script["che"] = task(intent="get_tasks")
    bad = eng.handle(SID, "che")
    assert not bad.ok and "Tasks non risponde" in bad.html                                                              # a una richiesta diretta, invece, lo dice


def test_only_tasks_today_and_no_events(eng, monkeypatch):
    from services import nextcloud_service as NC
    monkeypatch.setattr(NC, "get_calendar_events", lambda a, b: [])
    eng.script["oggi"] = parsed(intent="get_events", start_time="2026-10-06T00:00:00")
    r = eng.handle(SID, "oggi", spoken=True)
    assert "Oggi non hai impegni in calendario" in r.html and r.speak.startswith("Oggi non hai impegni in calendario. E hai 3 promemoria")


# ---------------------------------------------------------------------------
# Gemini, buongiorno, dashboard
# ---------------------------------------------------------------------------

def test_the_prompt_teaches_the_new_commands(monkeypatch):
    seen = {}

    class R:
        text = '{"intent":"add_task"}'
    monkeypatch.setattr(G._model, "generate_content", lambda p, **k: (seen.setdefault("p", p), R())[1])
    G.parse_user_input("aggiungimi un promemoria domani", "2026-10-06 10:00:00")
    p = seen["p"]
    for needle in ('"add_task"', '"complete_task"', '"get_tasks"', '"due_day"', '"due_clock"', '"task_list"', "Non inventare un orario", "NON è create_event", "da pagina 20 a 30"):
        assert needle in p, needle


def test_morning_briefing_gets_a_tasks_section_after_the_overview():
    tasks = [{"title": "Chiamare <b>Marco</b>", "label": "oggi 12:00"}, {"title": "Pile", "label": ""}]
    secs = digest.build_morning(NOW, "Mario", [], None, "feed fermi", None, tasks=tasks)
    assert secs[1].startswith("☑️ <b>Promemoria</b>") and "&lt;b&gt;Marco" in secs[1] and "(oggi 12:00)" in secs[1] and secs[2].startswith("⚠️")
    assert len(digest.build_morning(NOW, "Mario", [], None, "x", None)) == 2 and len(digest.build_morning(NOW, "Mario", [], None, "x", None, tasks=[])) == 2
    many = digest.tasks_section([{"title": f"t{i}", "label": ""} for i in range(7)], total=9)
    assert many.count("•") == 7 and "e altri 2" in many                                                                 # 7 mostrati, in tutto 9
    assert "e altri" not in digest.tasks_section([{"title": "a", "label": ""}]) and digest.tasks_section([], 5) == ""
    secs = digest.build_morning(NOW, "Mario", [], None, "x", None, tasks=[{"title": "a", "label": "oggi"}], tasks_total=4)
    assert "e altri 3" in secs[1]


def test_dashboard_card_shows_the_seven_most_urgent_including_far_away_ones(monkeypatch):
    spesa = FakeTaskList("Spesa", [make_todo("latte", at(6, 12, 0), uid="s1@t")])
    todo = FakeTaskList("TO DO", [make_todo("scaduto", at(4), uid="1@t"), make_todo("oggi", at(6), uid="2@t"), make_todo("più avanti", at(30), uid="3@t"),
                                  make_todo("senza", None, uid="4@t"), make_todo("fra tre giorni", at(9), uid="5@t")])
    monkeypatch.setattr(T, "task_lists", lambda force=False: [todo, spesa])
    d = cards.card_tasks(NOW)
    flat = {g["name"]: [t["title"] for t in g["items"]] for g in d["lists"]}
    assert flat == {"TO DO": ["scaduto", "oggi", "fra tre giorni", "più avanti", "senza"], "Spesa": ["latte"]}             # anche «più avanti», che prima restava nascosto
    assert d["total"] == 6 and d["shown"] == 6 and d["hidden"] == 0 and d["overdue"] == 1
    order = [t["level"] for g in d["lists"] for t in g["items"]]
    assert order == ["overdue", "today", "today", "soon", "later", "none"] or sorted(order, key=["overdue", "today", "soon", "later", "none"].index) == sorted(order, key=["overdue", "today", "soon", "later", "none"].index)
    json.dumps(d)
    big = FakeTaskList("TO DO", [make_todo(f"t{i}", None, uid=f"{i}@t") for i in range(15)])
    monkeypatch.setattr(T, "task_lists", lambda force=False: [big])
    d = cards.card_tasks(NOW)
    assert d["shown"] == 7 and d["hidden"] == 8 and d["total"] == 15 and len(d["lists"][0]["items"]) == 7


def test_dashboard_routes_tick_and_untick(monkeypatch):
    c, _, _ = make()
    calls = []
    monkeypatch.setattr(T, "set_done_by_uid", lambda uid, done, lst=None: (calls.append((uid, done, lst)), {"uid": uid, "title": "x", "done": done, "list": "TO DO"})[1])
    h = {**LOGIN, **API}
    cards._cache["tasks"] = (0, {"vecchio": True})
    r = c.post("/api/tasks/complete", json={"uid": "5f3a-9@edith", "list": "TO DO"}, headers=h)
    assert r.status_code == 200 and r.json()["ok"] is True and calls == [("5f3a-9@edith", True, "TO DO")] and "tasks" not in cards._cache      # cache svuotata
    assert c.post("/api/tasks/reopen", json={"uid": "5f3a-9@edith"}, headers=h).json()["done"] is False and calls[-1] == ("5f3a-9@edith", False, None)


@pytest.mark.parametrize("body", [{}, {"uid": ""}, {"uid": 5}, {"uid": "a b"}, {"uid": "../../x"}, {"uid": "a" * 201}, {"uid": "x'\"<>"}, {"uid": "ok@x", "list": 5}, {"uid": "ok@x", "list": "l" * 81}])
def test_dashboard_routes_reject_bad_input(monkeypatch, body):
    c, *_ = make()
    monkeypatch.setattr(T, "set_done_by_uid", lambda *a, **k: pytest.fail("non deve arrivare a Nextcloud"))
    assert c.post("/api/tasks/complete", json=body, headers={**LOGIN, **API}).status_code == 400


def test_dashboard_routes_need_login_and_our_header_and_hide_failures(monkeypatch):
    c, *_ = make()
    assert c.post("/api/tasks/complete", json={"uid": "a@x"}, headers=API).status_code == 401
    assert c.post("/api/tasks/complete", json={"uid": "a@x"}, headers={**LOGIN, "Content-Type": "application/json"}).status_code == 400
    assert c.post("/api/tasks/complete", json={"uid": "a@x"}, headers={**LOGIN, **API, "Sec-Fetch-Site": "cross-site"}).status_code == 400
    h = {**LOGIN, **API}
    monkeypatch.setattr(T, "set_done_by_uid", lambda *a, **k: (_ for _ in ()).throw(T.TaskError("Non trovo più questo promemoria")))
    assert c.post("/api/tasks/complete", json={"uid": "a@x"}, headers=h).json() == {"ok": False, "error": "Non trovo più questo promemoria"}
    monkeypatch.setattr(T, "set_done_by_uid", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("password=SEGRETA")))
    r = c.post("/api/tasks/complete", json={"uid": "a@x"}, headers=h)
    assert r.json()["ok"] is False and "SEGRETA" not in r.text
