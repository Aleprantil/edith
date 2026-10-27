"""Motore unico: lo sportello di Telegram (messaggi, pulsanti, spezzettamento), i permessi, e un giro completo con il motore vero."""
import asyncio
import re
import time

import pytest
from telegram.error import BadRequest, TelegramError

from bot import engine_bridge as EB
from services import assistant as A
from services import memory_service as M
from services import nextcloud_service as NC
from services import tasks_service as T
from services.assistant import Reply
from test_assistant import NOW, ev, parsed


# ---------------------------------------------------------------------------
# Finti oggetti di Telegram
# ---------------------------------------------------------------------------

class FakeStatus:
    def __init__(self):
        self.deleted = False

    async def delete(self):
        self.deleted = True


class FakeMessage:
    def __init__(self, text):
        self.text, self.replies, self.status = text, [], FakeStatus()

    async def reply_text(self, text, **kw):
        self.replies.append(text)
        return self.status


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def update(text, uid=42, chat=42):
    return Obj(message=FakeMessage(text), effective_user=Obj(id=uid), effective_chat=Obj(id=chat), callback_query=None)


class FakeBot:
    def __init__(self, fail_html=False):
        self.sent, self.fail_html = [], fail_html

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None, disable_web_page_preview=None):
        if parse_mode == "HTML" and self.fail_html:
            raise BadRequest("Can't parse entities: unsupported start tag")
        self.sent.append({"chat": chat_id, "text": text, "mode": parse_mode, "markup": reply_markup})
        return Obj(message_id=len(self.sent))


class FakeQuery:
    def __init__(self, data, uid=42, chat=42, edit_fails=False):
        self.data, self.from_user, self.message = data, Obj(id=uid), Obj(chat_id=chat)
        self.answers, self.markup_removed, self.edit_fails = [], 0, edit_fails

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_reply_markup(self, reply_markup=None):
        if self.edit_fails:
            raise TelegramError("message is not modified")
        assert reply_markup is None
        self.markup_removed += 1


def cb_update(data, **kw):
    q = FakeQuery(data, **kw)
    return Obj(callback_query=q), q


class FakeEngine:
    def __init__(self, features=None):
        self.features, self.calls, self.confirms, self.script, self.delay = features, [], [], {}, 0

    def handle(self, sid, text, spoken=False):
        if self.delay:
            time.sleep(self.delay)
        self.calls.append((sid, text))
        out = self.script.get(text)
        if isinstance(out, Exception):
            raise out
        return out or Reply(html=f"ok <b>{text}</b>")

    def confirm(self, sid, token, yes, spoken=False):
        self.confirms.append((sid, token, yes))
        return Reply(html="fatto" if yes else "annullato")


def bridge(feats=frozenset({"calendar", "finance"}), engine=None, **kw):
    engines = {}
    return EB.EngineBridge(lambda uid: feats if uid in (42, 7) else frozenset(), make_engine=lambda uid, f: engines.setdefault(uid, engine or FakeEngine(f)), **kw), engines


run = asyncio.run


# ---------------------------------------------------------------------------
# Permessi e forma delle risposte
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("uid,cal,fin,expected", [
    (42, {42}, {42, 7}, {"calendar", "finance"}), (7, {42}, {42, 7}, {"finance"}), (99, {42}, {42, 7}, set()),
    (99, set(), set(), {"calendar", "finance"}),                         # nessun limite configurato: come nel resto del bot
    (42, {42}, set(), {"calendar", "finance"}), (7, set(), {42}, {"calendar"})])
def test_features_follow_the_two_lists_of_users(uid, cal, fin, expected):
    assert EB.features_for_ids(uid, cal, fin) == frozenset(expected)


def test_telegram_html_has_no_br_and_no_unknown_link_attributes():
    assert EB.to_telegram_html('uno<br>due<br/>tre') == "uno\ndue\ntre"
    assert EB.to_telegram_html('<a href="https://x.it/a?b=1&amp;c=2" target="_blank" rel="noopener noreferrer">x</a>') == '<a href="https://x.it/a?b=1&amp;c=2">x</a>'
    assert EB.to_telegram_html("") == "…" and EB.to_telegram_html(None) == "…" and EB.to_telegram_html("  \n ") == "…"


def test_short_text_is_one_chunk_and_long_text_splits_at_line_breaks():
    assert EB.chunk_html("<b>ciao</b>\nmondo") == ["<b>ciao</b>\nmondo"]
    text = "\n".join(f"<b>riga {i}</b> " + "x" * 90 for i in range(200))
    chunks = EB.chunk_html(text)
    assert len(chunks) > 3 and all(len(c) <= EB.MAX_LEN for c in chunks)
    assert "\n".join(chunks) == text                                       # niente perso, niente a metà
    assert all(c.count("<b>") == c.count("</b>") for c in chunks)          # i tag restano interi


def test_one_enormous_line_becomes_safe_plain_text():
    line = "<b>" + "parola " * 2000 + "</b> & <fine>"
    chunks = EB.chunk_html(line)
    assert len(chunks) > 3 and all(len(c) <= EB.MAX_LEN for c in chunks)
    assert not any("<b>" in c for c in chunks) and any("&amp;" in c for c in chunks)         # testo, con i caratteri speciali protetti
    assert EB.chunk_html("") == ["…"] and EB.chunk_html("\n\n") == ["…"]


def test_keyboard_yes_no_and_choices():
    assert EB.keyboard(None) is None
    k = EB.keyboard({"token": "abcDEF123xyz", "yes": "Sposta", "no": "Annulla"})
    row = k.inline_keyboard[0]
    assert [b.text for b in row] == ["✅ Sposta", "✖️ Annulla"] and [b.callback_data for b in row] == ["eng:abcDEF123xyz:1", "eng:abcDEF123xyz:0"]
    k = EB.keyboard({"token": "CANCEL000000", "yes": "", "no": "Annulla", "choices": [{"token": "tok1aaaaaaaa", "label": "da pagina 10 a 20"}, {"token": "tok2bbbbbbbb", "label": "x" * 90}]})
    rows = k.inline_keyboard
    assert len(rows) == 3 and rows[0][0].text == "1. da pagina 10 a 20" and rows[0][0].callback_data == "eng:tok1aaaaaaaa:1"
    assert len(rows[1][0].text) <= 43 and rows[2][0].text == "✖️ Annulla" and rows[2][0].callback_data == "eng:CANCEL000000:0"
    assert all(len(b.callback_data.encode()) <= 64 and EB.CALLBACK_RE.match(b.callback_data) for r in rows for b in r)


def test_real_engine_tokens_always_fit_the_button_data_limit():
    token = A.Assistant()._new_token(A._Session(), lambda: None)
    assert len(f"eng:{token}:1".encode()) <= 64 and EB.CALLBACK_RE.match(f"eng:{token}:1")


# ---------------------------------------------------------------------------
# Invio
# ---------------------------------------------------------------------------

def test_reply_is_sent_as_html_with_buttons_on_the_last_message_only():
    bot = FakeBot()
    long_html = "\n".join(f"riga {i} " + "y" * 100 for i in range(120))
    run(EB.send_reply(bot, 42, Reply(html=long_html, confirm={"token": "abcDEF123xyz", "yes": "Sì", "no": "No"})))
    assert len(bot.sent) > 1 and all(m["mode"] == "HTML" and m["chat"] == 42 for m in bot.sent)
    assert all(m["markup"] is None for m in bot.sent[:-1]) and bot.sent[-1]["markup"] is not None


def test_if_telegram_rejects_the_html_it_falls_back_to_plain_text_and_keeps_the_buttons():
    bot = FakeBot(fail_html=True)
    run(EB.send_reply(bot, 42, Reply(html="uno <b>due</b> &amp; tre", confirm={"token": "abcDEF123xyz", "yes": "Sì", "no": "No"})))
    assert bot.sent == [{"chat": 42, "text": "uno due & tre", "mode": None, "markup": bot.sent[0]["markup"]}] and bot.sent[0]["markup"] is not None


# ---------------------------------------------------------------------------
# Lo sportello
# ---------------------------------------------------------------------------

def test_a_message_goes_to_the_engine_and_the_answer_comes_back():
    br, engines = bridge()
    u, bot = update("cosa faccio oggi", chat=555), FakeBot()
    run(br.on_text(u, Obj(bot=bot)))
    assert engines[42].calls == [("tg-555", "cosa faccio oggi")]                       # una conversazione per chat
    assert u.message.replies == ["⏳ Elaboro..."] and u.message.status.deleted
    assert bot.sent[0]["text"] == "ok <b>cosa faccio oggi</b>" and bot.sent[0]["chat"] == 555


def test_unauthorized_users_and_empty_messages_never_reach_the_engine():
    br, engines = bridge()
    u, bot = update("ciao", uid=99), FakeBot()
    run(br.on_text(u, Obj(bot=bot)))
    assert u.message.replies == ["⛔ Non sei autorizzato."] and bot.sent == [] and engines == {}
    for empty in ("", "   ", None):
        run(br.on_text(update(empty), Obj(bot=bot)))
    assert bot.sent == [] and engines == {}
    run(br.on_text(Obj(message=None, effective_user=None, effective_chat=None), Obj(bot=bot)))


def test_every_person_gets_their_own_engine_with_their_own_permissions():
    made = []
    br = EB.EngineBridge(lambda uid: {42: frozenset({"calendar", "finance"}), 7: frozenset({"finance"})}.get(uid, frozenset()),
                         make_engine=lambda uid, f: (made.append((uid, f)), FakeEngine(f))[1])
    run(br.on_text(update("a", uid=42), Obj(bot=FakeBot())))
    run(br.on_text(update("b", uid=7, chat=7), Obj(bot=FakeBot())))
    run(br.on_text(update("c", uid=42), Obj(bot=FakeBot())))
    assert made == [(42, frozenset({"calendar", "finance"})), (7, frozenset({"finance"}))]                # riuso, non uno per messaggio
    assert br.engine(42, {"calendar", "finance"}) is not br.engine(7, {"finance"})


def test_slow_or_failing_engine_gives_a_clear_message_not_silence():
    eng = FakeEngine(); eng.delay = 0.6
    br, _ = bridge(engine=eng, timeout=0.2)
    u, bot = update("lento"), FakeBot()
    run(br.on_text(u, Obj(bot=bot)))
    assert "troppo" in bot.sent[0]["text"] and u.message.status.deleted
    eng2 = FakeEngine(); eng2.script["rotto"] = RuntimeError("password=SEGRETA")
    br2, _ = bridge(engine=eng2)
    bot2 = FakeBot()
    run(br2.on_text(update("rotto"), Obj(bot=bot2)))
    assert "andato storto" in bot2.sent[0]["text"] and "SEGRETA" not in bot2.sent[0]["text"]


def test_messages_from_the_same_person_are_handled_one_at_a_time_in_order():
    eng = FakeEngine(); eng.delay = 0.15
    br, _ = bridge(engine=eng)

    async def two():
        bot = FakeBot()
        await asyncio.gather(br.on_text(update("primo"), Obj(bot=bot)), br.on_text(update("secondo"), Obj(bot=bot)))
        return bot
    bot = run(two())
    assert [c[1] for c in eng.calls] == ["primo", "secondo"] and [m["text"] for m in bot.sent] == ["ok <b>primo</b>", "ok <b>secondo</b>"]


def test_nextcloud_is_warmed_up_only_for_people_who_use_the_calendar_and_never_fails_the_message():
    warm = []
    br, _ = bridge(prewarm=lambda: warm.append(1))
    run(br.on_text(update("x"), Obj(bot=FakeBot())))
    time.sleep(0.1)
    assert warm == [1]
    br2, _ = bridge(feats=frozenset({"finance"}), prewarm=lambda: warm.append(2))
    run(br2.on_text(update("x"), Obj(bot=FakeBot())))
    time.sleep(0.1)
    assert warm == [1]                                                                    # solo finanze: non serve
    br3, _ = bridge(prewarm=lambda: (_ for _ in ()).throw(OSError("giù")))
    bot = FakeBot()
    run(br3.on_text(update("x"), Obj(bot=bot)))
    assert bot.sent[0]["text"] == "ok <b>x</b>"


# ---------------------------------------------------------------------------
# Pulsanti
# ---------------------------------------------------------------------------

def test_a_button_press_confirms_removes_the_buttons_and_answers():
    br, engines = bridge()
    upd, q = cb_update("eng:abcDEF123xyz:1", chat=555)
    bot = FakeBot()
    run(br.on_callback(upd, Obj(bot=bot)))
    assert engines[42].confirms == [("tg-555", "abcDEF123xyz", True)] and q.markup_removed == 1 and q.answers == [(None, False)]
    assert bot.sent[0]["text"] == "fatto" and bot.sent[0]["chat"] == 555
    upd, q = cb_update("eng:abcDEF123xyz:0", chat=555)
    run(br.on_callback(upd, Obj(bot=bot)))
    assert engines[42].confirms[-1] == ("tg-555", "abcDEF123xyz", False) and bot.sent[-1]["text"] == "annullato"


def test_foreign_or_malformed_callbacks_are_ignored_and_strangers_are_refused():
    br, engines = bridge()
    bot = FakeBot()
    for data in ("cat_ristoranti", "obj_menu", "eng:", "eng:abc:1", "eng:abcDEF123xyz:2", "eng:abc DEF123xyz:1", "eng:" + "a" * 41 + ":1", "", None, "x" * 100):
        upd, q = cb_update(data)
        run(br.on_callback(upd, Obj(bot=bot)))
        assert q.answers == [(None, False)] and q.markup_removed == 0                       # solo l'orologio tolto: le finanze hanno i loro pulsanti
    assert engines == {} and bot.sent == []
    upd, q = cb_update("eng:abcDEF123xyz:1", uid=99)
    run(br.on_callback(upd, Obj(bot=bot)))
    assert q.answers == [("⛔ Non sei autorizzato.", True)] and bot.sent == [] and engines == {}
    run(br.on_callback(Obj(callback_query=None), Obj(bot=bot)))


def test_a_message_that_cannot_be_edited_does_not_stop_the_confirmation():
    br, engines = bridge()
    upd, q = cb_update("eng:abcDEF123xyz:1", edit_fails=True)
    bot = FakeBot()
    run(br.on_callback(upd, Obj(bot=bot)))
    assert engines[42].confirms and bot.sent[0]["text"] == "fatto"


# ---------------------------------------------------------------------------
# Un giro completo con il motore vero
# ---------------------------------------------------------------------------

@pytest.fixture
def real(monkeypatch):
    script = {}
    monkeypatch.setattr(A, "parse_user_input", lambda text, now_str, context="", **kw: script.get(text) or parsed(intent="unknown"))
    monkeypatch.setattr(A, "speech_rewrite", lambda plain, intent="": "")
    br = EB.EngineBridge(lambda uid: frozenset({"calendar", "finance"}) if uid == 42 else frozenset({"finance"}) if uid == 7 else frozenset(),
                         make_engine=lambda uid, f: A.Assistant(owner_id=uid, clock=lambda: NOW, features=f))
    return br, script


def press(br, bot, data, **kw):
    upd, q = cb_update(data, **kw)
    run(br.on_callback(upd, Obj(bot=bot)))
    return q


def tokens_of(markup):
    return [b.callback_data for r in markup.inline_keyboard for b in r]


def test_deleting_an_event_asks_with_buttons_and_only_then_does_it(real, monkeypatch):
    br, script = real
    gone = []
    monkeypatch.setattr(NC, "find_event", lambda *a, **k: (ev("Dentista", 15), "OGG"))
    monkeypatch.setattr(NC, "delete_found", lambda e, d: gone.append(d["title"]))
    script["cancella il dentista"] = parsed(intent="delete_event", title="Dentista")
    bot = FakeBot()
    run(br.on_text(update("cancella il dentista"), Obj(bot=bot)))
    assert gone == [] and "Cancello" in bot.sent[0]["text"] and bot.sent[0]["mode"] == "HTML"                  # prima chiede
    yes, no = tokens_of(bot.sent[0]["markup"])
    q = press(br, bot, yes)
    assert gone == ["Dentista"] and q.markup_removed == 1 and "cancellato" in bot.sent[-1]["text"]
    press(br, bot, yes)                                                                                   # secondo tocco sullo stesso pulsante
    assert gone == ["Dentista"] and "scaduta" in bot.sent[-1]["text"]                                          # non lo fa due volte


def test_saying_no_does_nothing(real, monkeypatch):
    br, script = real
    gone = []
    monkeypatch.setattr(NC, "find_event", lambda *a, **k: (ev("Dentista", 15), "OGG"))
    monkeypatch.setattr(NC, "delete_found", lambda e, d: gone.append(1))
    script["cancella"] = parsed(intent="delete_event", title="Dentista")
    bot = FakeBot()
    run(br.on_text(update("cancella"), Obj(bot=bot)))
    press(br, bot, tokens_of(bot.sent[0]["markup"])[1])
    assert gone == [] and "non faccio nulla" in bot.sent[-1]["text"]


def test_several_matches_become_one_button_each(real, monkeypatch):
    from web.demo import FakeTaskList, make_todo
    br, script = real
    lista = FakeTaskList("TO DO", [make_todo("da pagina 10 a 20", None, uid="a@t"), make_todo("da pagina 20 a 30", None, uid="b@t")])
    monkeypatch.setattr(T, "task_lists", lambda force=False: [lista])
    script["spunta pagina"] = {**parsed(intent="complete_task", title="pagina"), "task_list": None}
    bot = FakeBot()
    run(br.on_text(update("spunta pagina"), Obj(bot=bot)))
    labels = [b.text for r in bot.sent[0]["markup"].inline_keyboard for b in r]
    assert labels == ["1. da pagina 10 a 20", "2. da pagina 20 a 30", "✖️ Annulla"] and not any(t.done for t in lista.items)
    press(br, bot, tokens_of(bot.sent[0]["markup"])[1])
    assert [t.done for t in lista.items] == [False, True] and "Spuntato" in bot.sent[-1]["text"]
    assert [b.text for r in bot.sent[-1]["markup"].inline_keyboard for b in r] == ["✅ Annulla", "✖️ Ok"]       # e si può annullare


def test_the_two_front_ends_share_the_same_engine_behaviour(real, monkeypatch):
    """Una cosa sola da mantenere: la stessa frase dà la stessa risposta su Telegram e sulla pagina."""
    br, script = real
    monkeypatch.setattr(NC, "get_calendar_events", lambda a, b: [ev("Dentista", 15)])
    script["oggi"] = parsed(intent="get_events", start_time="2026-10-05T00:00:00")
    bot = FakeBot()
    run(br.on_text(update("oggi"), Obj(bot=bot)))
    web_reply = A.Assistant(owner_id=42, clock=lambda: NOW).handle("web-1", "oggi")
    assert re.sub(r"<[^>]+>", "", bot.sent[0]["text"]) == re.sub(r"<[^>]+>", "", EB.to_telegram_html(web_reply.html))


# ---------------------------------------------------------------------------
# Permessi nel motore
# ---------------------------------------------------------------------------

@pytest.fixture
def seen(monkeypatch):
    got, script = [], {}
    monkeypatch.setattr(A, "parse_user_input", lambda text, now_str, context="", **kw: (got.append(kw), script.get(text) or parsed(intent="unknown"))[1])
    monkeypatch.setattr(M, "prompt_block", lambda: "- sono mancino")
    return got, script


def test_finance_only_people_cannot_touch_the_calendar(seen, monkeypatch):
    got, script = seen
    monkeypatch.setattr(NC, "get_calendar_events", lambda a, b: pytest.fail("non deve leggere il calendario"))
    script["agenda"] = parsed(intent="get_events", start_time="2026-10-05T00:00:00")
    script["note"] = parsed(intent="add_note", title="x")
    script["server"] = parsed(intent="get_server_status")
    script["promemoria"] = {**parsed(intent="add_task", title="x"), "task_list": None, "due_day": None, "due_clock": None}
    eng = A.Assistant(owner_id=7, clock=lambda: NOW, features={"finance"})
    for text, what in (("agenda", "calendario"), ("note", "calendario"), ("server", "calendario"), ("promemoria", "calendario")):
        r = eng.handle("s", text)
        assert not r.ok and "Non sei autorizzato a usare il calendario" in r.html, text


def test_finance_only_people_can_do_finance_and_never_see_the_memory(seen, monkeypatch):
    got, script = seen
    calls = []
    monkeypatch.setattr(A, "_finance", lambda: type("F", (), {"add_expense": staticmethod(lambda *a: _coro(calls.append(a) or "✅ Spesa registrata"))})())
    script["pizza"] = parsed(intent="add_expense", amount=12.0, finance_category="ristoranti", title="pizza")
    eng = A.Assistant(owner_id=7, clock=lambda: NOW, features={"finance"})
    r = eng.handle("s", "pizza")
    assert r.ok and "Spesa registrata" in r.html and calls and calls[0][0] == 7                                   # le sue spese, con il suo id
    assert got == [{}]                                                                                         # la memoria (tua) non è finita nella richiesta
    r = eng.handle("s", "ricordati che sono mancino")                                                         # e il comando non lo riconosce nemmeno
    assert M.read(force=True) == [] and "finanze" in r.html and got[-1] == {}
    assert "Non ho capito" in eng.handle("s", "boh").html


async def _coro(value):
    return value


def test_calendar_people_cannot_touch_finance_and_everyone_with_none_set_can_do_all(seen, monkeypatch):
    got, script = seen
    script["pizza"] = parsed(intent="add_expense", amount=12.0, finance_category="ristoranti", title="pizza")
    r = A.Assistant(owner_id=42, clock=lambda: NOW, features={"calendar"}).handle("s", "pizza")
    assert not r.ok and "Non sei autorizzato alle finanze" in r.html
    assert got[-1] == {"memory": "- sono mancino"}                                                              # chi usa il calendario la memoria la ha
    full = A.Assistant(owner_id=42, clock=lambda: NOW)
    assert full._can("calendar") and full._can("finance") and A.Assistant(features=set())._can("calendar") is False
