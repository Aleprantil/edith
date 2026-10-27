"""
Anteprima della web app con DATI FINTI (nessun Nextcloud, Gemini, Docker o Telegram necessario).

    python3 -m web.demo            # poi apri http://127.0.0.1:8788

Serve per vedere l'aspetto e provare la conversazione. Gli eventi che crei, sposti o cancelli restano in memoria
finché il programma è acceso. NON usa i tuoi dati.
"""
import asyncio
import os
import sys
import types
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000:demo")
os.environ.setdefault("GEMINI_API_KEY", "demo")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
os.environ.setdefault("OWNER_NAME", "Mario")

TZ = ZoneInfo("Europe/Rome")


# ---------------------------------------------------------------------------
# Moduli del FinanceBot finti (la web app li importa solo se esistono)
# ---------------------------------------------------------------------------
def _install_finance_stubs():
    C = types.ModuleType("constants")
    C.CATEGORIE_SPESA = {"🍕 Ristoranti": "Ristoranti", "🚗 Trasporti": "trasporti", "🏠 Spesa": "spesa", "❓ Altro": "altro"}
    C.TIPI_ENTRATA = {"💼 Stipendio": "stipendio", "❓ Altro": "altro_entrata"}
    emoji = {"Ristoranti": "🍕", "trasporti": "🚗", "spesa": "🏠", "altro": "❓", "stipendio": "💼", "tech": "💻", "svago": "🎬"}
    label = {"Ristoranti": "Ristoranti", "trasporti": "Trasporti", "spesa": "Spesa", "altro": "Altro", "stipendio": "Stipendio", "tech": "Tech", "svago": "Svago"}
    C.categoria_display = lambda c: f"{emoji.get(c, '❓')} {label.get(c, c)}"
    C.entrata_display = lambda c: f"{emoji.get(c, '💸')} {label.get(c, c)}"
    C.fmt_amount = lambda a, cur="€": f"{cur}{a:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    sys.modules["constants"] = C

    D = types.ModuleType("database")
    today = datetime.now()
    D.get_category_totals = lambda uid, a, b, kind="spesa": (
        [{"category": "Ristoranti", "total": 118.5}, {"category": "spesa", "total": 96.2}, {"category": "trasporti", "total": 64.0},
         {"category": "tech", "total": 39.9}, {"category": "svago", "total": 22.0}] if kind == "spesa"
        else [{"category": "stipendio", "total": 1500.0}])
    D.get_transactions = lambda uid, a, b: [
        {"created_at": (today - timedelta(days=i)).strftime("%Y-%m-%d 12:00:00"), "type": t, "category": c, "amount": amt, "note": n}
        for i, (t, c, amt, n) in enumerate([("spesa", "Ristoranti", 18.0, "sushi"), ("spesa", "trasporti", 30.0, "benzina"),
                                            ("spesa", "spesa", 42.7, "Esselunga"), ("entrata", "stipendio", 1500.0, "settembre"),
                                            ("spesa", "tech", 39.9, "cavo USB-C")])]
    def daily(uid, a, b, kind="spesa"):
        import random
        rnd = random.Random(7 if kind == "spesa" else 9)
        from datetime import date as _d, timedelta as _td
        d0, d1, out = _d.fromisoformat(a), _d.fromisoformat(b), []
        while d0 <= d1:
            if kind == "spesa" and rnd.random() < .62:
                out.append({"day": d0.isoformat(), "total": round(rnd.uniform(6, 48), 2)})
            elif kind != "spesa" and d0.day == 2:
                out.append({"day": d0.isoformat(), "total": 1500.0})
            d0 += _td(days=1)
        return out
    D.get_daily_totals = daily
    D.get_budgets = lambda uid: [{"category": "Ristoranti", "monthly_limit": 150.0}, {"category": "spesa", "monthly_limit": 250.0},
                                 {"category": "trasporti", "monthly_limit": 60.0}]
    sys.modules["database"] = D

    F = types.ModuleType("services.finance_nl")

    async def add_expense(uid, amount, cat, note, ts):
        return f"✅ Spesa registrata!\n*🍕 Ristoranti* — €{float(amount):.2f}".replace(".", ",") + (f"\n📝 _{note}_" if note else "")

    async def add_income(uid, amount, cat, note, ts):
        return f"✅ Entrata registrata!\n*💼 Stipendio* — €{float(amount):.2f} 🎉".replace(".", ",")

    async def get_summary(uid, period, cat=None, a=None, b=None):
        return "📊 *Riepilogo ottobre 2026*\n\n💚 Entrate:  €1.500,00\n🔴 Spese:    €340,60\n💰 Saldo:     €1.159,40\n\n📂 *Spese per categoria:*\n  🍕 Ristoranti ████░░░░ €118,50"
    F.add_expense, F.add_income, F.get_summary = add_expense, add_income, get_summary
    sys.modules["services.finance_nl"] = F


# ---------------------------------------------------------------------------
# Calendario finto in memoria
# ---------------------------------------------------------------------------
class _Cal:
    def __init__(self):
        self.events = []
        t = datetime.combine(datetime.now(TZ).date(), dtime.min, tzinfo=TZ)
        for day, h, m, dur, title, cal, loc in [
            (0, 9, 0, 60, "Lezione di Software Engineering", "università", "Aula A104"),
            (0, 11, 30, 90, "Call con il gruppo del progetto", "università", ""),
            (0, 15, 0, 60, "Dentista", "svago", "Via Roma 12"),
            (0, 18, 30, 90, "Allenamento", "calcio", "Campo sportivo"),
            (1, 8, 0, 45, "Colazione al bar", "svago", "Bar Centrale"),
            (1, 14, 0, 120, "Esame di Sistemi Operativi", "esami", "Aula 3"),
            (2, 19, 0, 120, "Cena con Marco", "svago", ""),
        ]:
            s = t + timedelta(days=day, hours=h, minutes=m)
            self.events.append({"title": title, "start": s, "end": s + timedelta(minutes=dur), "calendar": cal, "location": loc, "recurring": False})

    def between(self, a, b):
        return sorted([e for e in self.events if a <= e["start"] < b], key=lambda e: e["start"])


CAL = _Cal()


def _install_fake_services():
    from services import monitor_service, news_service, nextcloud_service, notes_service, weather_service
    from services import assistant as asst

    nextcloud_service.get_calendar_events = lambda a, b: [dict(e) for e in CAL.between(a, b)]

    def create(title, start_time_str, description="", calendar_name="generale", rrule=None, end_time_str=None, location=""):
        s = datetime.fromisoformat(start_time_str)
        s = s if s.tzinfo else s.replace(tzinfo=TZ)
        e = datetime.fromisoformat(end_time_str).replace(tzinfo=TZ) if end_time_str else s + timedelta(hours=1)
        clash = any(x["start"] < e and s < x["end"] for x in CAL.events)
        CAL.events.append({"title": title, "start": s, "end": e, "calendar": calendar_name or "svago", "location": location, "recurring": False})
        return {"success": True, "calendar_used": calendar_name or "svago", "conflict": clash, "location": location}
    nextcloud_service.create_calendar_event = create

    def find(calendar_name, search_dt, title, days=1, action="modificare"):
        a = datetime.combine(search_dt.date(), dtime.min, tzinfo=TZ)
        for e in CAL.between(a, a + timedelta(days=days)):
            if title.lower() in e["title"].lower():
                return dict(e), e
        return None
    nextcloud_service.find_event = find
    nextcloud_service.delete_found = lambda ev, d: CAL.events.remove(ev)

    def update(ev, d, new_start, new_end=None):
        dur = ev["end"] - ev["start"]
        ev["start"] = new_start if new_start.tzinfo else new_start.replace(tzinfo=TZ)
        ev["end"] = new_end or ev["start"] + dur
    nextcloud_service.update_found = update
    nextcloud_service.get_free_slots = lambda dt, cal="lavoro": [(dt.replace(hour=12, minute=0), dt.replace(hour=15, minute=0))]

    _base = datetime.now(TZ).date()
    weather_service.get_forecast = lambda force=False: [
        {"date": str(_base), "tmin": 9, "tmax": 19, "pop": 20, "precip": 0, "label": "parzialmente nuvoloso", "emoji": "⛅", "sunrise": "07:12", "sunset": "18:03"},
        {"date": str(_base + timedelta(days=1)), "tmin": 8, "tmax": 14, "pop": 70, "precip": 6, "label": "pioggia", "emoji": "🌧️", "sunrise": "07:13", "sunset": "18:01"},
        {"date": str(_base + timedelta(days=2)), "tmin": 7, "tmax": 16, "pop": 10, "precip": 0, "label": "sereno", "emoji": "☀️", "sunrise": "07:15", "sunset": "17:59"}]

    now = datetime.now().timestamp()
    notes = [{"title": "Spesa", "modified": now - 3600 * 5}, {"title": "Idee per la tesi", "modified": now - 86400 * 2},
             {"title": "Regali di Natale", "modified": now - 86400 * 9}]
    notes_service.list_notes = lambda cat="": [dict(n) for n in notes]
    contents = {"Spesa": "# Spesa\n*Creata il: 05/10/2026 09:00*\n\nlatte, uova, pane, caffè", "Idee per la tesi": "# Idee\n\n- confronto tra scheduler\n- misure su Linux",
                "Regali di Natale": "# Regali\n\nmamma: sciarpa · papà: libro"}
    notes_service.get_note_content = lambda title, cat="": contents.get(title)
    notes_service.save_note_to_nextcloud = lambda title, content, cat="": (notes.insert(0, {"title": title, "modified": datetime.now().timestamp()}),
                                                                           contents.__setitem__(title, f"# {title}\n\n{content}"),
                                                                           {"success": True, "title": title, "folder": "Note"})[2]

    def grp(folder, items):
        return {"folder": folder, "feed": "", "all_ids": [], "items": [
            {"id": f"{folder}{i}", "title": t, "line": t, "url": "https://example.com/" + folder + str(i), "pub": now - 3600 * h, "source": src}
            for i, (t, src, h) in enumerate(items)]}
    groups = [grp("sport", [("Inter-Milan 2-1: decide Lautaro al 90'", "Gazzetta", 1), ("Il Napoli blinda il centrocampo", "Gazzetta", 3), ("Mondiali 2026: i convocati di Spalletti", "Gazzetta", 5)]),
              grp("mondo", [("Vertice UE sull'energia: accordo vicino", "ANSA", 2), ("Elezioni in Brasile: affluenza record", "BBC", 4), ("Nuovo accordo commerciale tra USA e India", "BBC", 6)]),
              grp("cyber", [("Vulnerabilità critica in un plugin di WordPress", "The Hacker News", 2), ("Ransomware: attacco a un ospedale europeo", "The Hacker News", 7)])]
    news_service.collect_digest = lambda *a, **k: groups
    news_service.collect_latest = lambda topic=None, topic_items=8: (groups, None)

    from services import tasks_service
    now_ = datetime.now(TZ)
    tomorrow_end = datetime.combine(now_.date() + timedelta(days=1), dtime(23, 59), tzinfo=TZ)
    LIST = FakeTaskList("TO DO", [
        make_todo("Chiamare il commercialista", datetime.combine(now_.date() - timedelta(days=1), dtime(18, 0), tzinfo=TZ), uid="t1@demo"),
        make_todo("Comprare le pile", datetime.combine(now_.date(), dtime(23, 59), tzinfo=TZ), uid="t2@demo"),
        make_todo("da pagina 10 a 20", tomorrow_end, uid="t3@demo"),
        make_todo("Rinnovare l'abbonamento", None, uid="t4@demo"),
        make_todo("Leggere l'articolo salvato", None, uid="t5@demo"),
        make_todo("Pagare la bolletta", None, done=True, uid="t6@demo"),
        make_todo("Prenotare la visita", datetime.combine(now_.date() + timedelta(days=12), dtime(23, 59), tzinfo=TZ), uid="t7@demo"),
        make_todo("Rinnovare il passaporto", datetime.combine(now_.date() + timedelta(days=20), dtime(23, 59), tzinfo=TZ), uid="t8@demo"),
        make_todo("Studiare il capitolo 3", None, uid="t9@demo"),
    ])
    OTHER = FakeTaskList("università", [make_todo("Consegnare la tesi (altra lista)", None, uid="o1@demo")], components=("VEVENT", "VTODO"))
    tasks_service._calendars = lambda: [OTHER, LIST]             # le liste vere passano dal filtro: «università» non deve comparire
    tasks_service._lists_cache.update(at=0, cals=None, all=[])
    tasks_service.FAKE_LIST = LIST

    class FakeMon:
        def card(self):
            return {"host": "edith-server", "uptime_s": 3 * 86400 + 5 * 3600, "cpu": {"pct": 12.4, "avg": 9.0, "cores": 2, "load": [0.45, 0.38, 0.30]},
                    "mem": {"used": 1.4 * 2 ** 30, "total": 3.8 * 2 ** 30, "pct": 37.0, "swap_used": 0, "swap_total": 2 * 2 ** 30},
                    "temp": {"cpu": 54.0, "peak": 61.0, "fans": [2800], "disks": [{"dev": "/dev/sda", "temp": 45}]},
                    "root": {"total": 293 * 2 ** 30, "used": 27 * 2 ** 30, "free": 266 * 2 ** 30, "pct": 9.2},
                    "mounts": [{"path": "/mnt/data", "mounted": True, "ro": False, "used": 8 * 2 ** 30, "total": 234 * 2 ** 30, "pct": 3.4, "fstype": "ext4"}],
                    "power": {"ac": True, "battery": {"pct": 100, "status": "Full"}}, "net": [{"name": "enp2s0", "up": True}, {"name": "tailscale0", "up": True}],
                    "nextcloud": {"ok": True, "why": "", "ms": 48}, "docker": [{"name": "cloud-foto", "state": "running"}, {"name": "telegram_assistant", "state": "running"}],
                    "kernel": {"usb": 0, "io": 0, "fs": 0, "power": 0}, "smart": [{"dev": "/dev/sda", "status": "PASSED", "timeout": False}, {"dev": "/dev/sdb", "status": "SKIPPED", "timeout": False}],
                    "alerts": [], "probe": True, "probe_age": 20, "bot_mem": 95 * 2 ** 20}

        def server_report(self):
            return "🖥️ <b>Server</b> · edith-server\n⏱️ Acceso da 3 giorni 5 h\n🧠 <b>CPU</b> 12% · 2 core\n🌡️ <b>Temperature</b> CPU 54°C\n💾 <b>RAM</b> 1.4 GB / 3.8 GB (37%)\n☁️ <b>Nextcloud</b> ✅ risponde (48 ms)"
    monitor_service.MONITOR = FakeMon()

    # Gemini finto: riconosce poche parole chiave
    def parse(text, now_str, context=""):
        t = text.lower()
        base = {"intent": "unknown", "title": "", "start_time": None, "end_time": None, "location": "", "category": "generale", "rrule": None,
                "description": "", "original_time": None, "amount": None, "finance_category": None, "period": None, "news_topic": None}
        tomorrow = datetime.now(TZ).date() + timedelta(days=1)
        tomorrow_iso = str(tomorrow)
        if "promemoria" in t and any(w in t for w in ("aggiung", "ricord")):
            return {**base, "intent": "add_task", "title": "da pagina 20 a 30", "due_day": tomorrow_iso}
        if "spunta" in t:
            return {**base, "intent": "complete_task", "title": "pagina" if "tutte" in t else ("da pagina 20 a 30" if "pagina" in t else "commercialista")}
        if "che promemoria" in t or "cosa devo fare" in t:
            return {**base, "intent": "get_tasks"}
        if "sposta" in t:
            return {**base, "intent": "update_event", "title": "Dentista", "original_time": datetime.now(TZ).strftime("%Y-%m-%dT00:00:00"),
                    "start_time": f"{tomorrow}T10:00:00", "category": "svago"}
        if "cancella" in t or "elimina" in t:
            return {**base, "intent": "delete_event", "title": "Allenamento", "original_time": datetime.now(TZ).strftime("%Y-%m-%dT00:00:00")}
        if "segna" in t or "aggiungi" in t:
            return {**base, "intent": "create_event", "title": "Pranzo con Anna", "start_time": f"{tomorrow}T13:00:00", "category": "svago", "location": "Bar Centrale"}
        if "nota" in t:
            return {**base, "intent": "add_note", "title": "Idea", "description": text}
        if "speso" in t and any(ch.isdigit() for ch in t):
            return {**base, "intent": "add_expense", "amount": 12, "finance_category": "ristoranti", "title": "pizza"}
        if "quanto ho speso" in t or "spese" in t:
            return {**base, "intent": "get_finance_summary", "period": "mese"}
        if "news" in t or "notizie" in t:
            return {**base, "intent": "get_news"}
        if "server" in t or "ram" in t:
            return {**base, "intent": "get_server_status"}
        if "libero" in t:
            return {**base, "intent": "find_free_slots", "start_time": f"{tomorrow}T00:00:00"}
        if "oggi" in t or "faccio" in t:
            today = datetime.now(TZ).strftime("%Y-%m-%dT00:00:00")
            return {**base, "intent": "get_events", "start_time": today}
        return base
    asst.parse_user_input = parse

    heard = [x.strip() for x in os.environ.get("DEMO_HEARD", "cosa faccio oggi").split(",") if x.strip()]

    def fake_transcribe(wav):                      # niente Gemini: «sente» le frasi indicate in DEMO_HEARD, una per volta
        open("/tmp/demo_last_voice.wav", "wb").write(wav)
        return heard.pop(0) if len(heard) > 1 else heard[0]
    asst.transcribe_audio = fake_transcribe
    asst.speech_rewrite = lambda plain, intent="": "Tutto bene: il server funziona e Nextcloud risponde."


# ---------------------------------------------------------------------------
# Lista di promemoria finta, con voci iCalendar vere (così gira il codice vero di lettura e scrittura)
# ---------------------------------------------------------------------------
class FakeTodo:
    def __init__(self, ical):
        import vobject
        self.vobject_instance = vobject.readOne(ical)
        self.saves = 0
        self.completed_calls = 0

    def save(self, **kw):
        self.saves += 1
        return self

    def complete(self, handle_rrule=False, **kw):
        self.completed_calls += 1

    @property
    def done(self):
        v = self.vobject_instance.vtodo
        return hasattr(v, "status") and str(v.status.value).upper() == "COMPLETED"


class FakeTaskList:
    def __init__(self, name, items=(), components=("VTODO",)):
        self.name, self.components, self.items, self.last_ical = name, list(components), list(items), None

    def get_supported_components(self):
        return self.components

    def todos(self, include_completed=False, **kw):
        return [t for t in self.items if include_completed or not t.done]

    def save_todo(self, ical=None, **kw):
        self.last_ical = ical
        t = FakeTodo(ical)
        self.items.append(t)
        return t


def make_todo(title, due=None, done=False, uid=None):
    from services import tasks_service
    t = FakeTodo(tasks_service.build_ical(uid or f"{abs(hash(title)) % 10**8}@demo", title, due))
    if done:
        tasks_service.set_done(t, True)
    return t


class _InjectLogin:
    """Nella demo non c'è Tailscale: aggiungo io l'header di identità."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope["headers"] = [(k, v) for k, v in scope["headers"] if k != b"tailscale-user-login"] + [(b"tailscale-user-login", b"demo@example.com")]
        await self.app(scope, receive, send)


def _install_faults():
    """DEMO_FAULTS=1: simula i guasti, per vedere come si presenta la pagina quando qualcosa non va."""
    from services import monitor_service, news_service, nextcloud_service, notes_service

    def calendar_down(*a, **k):
        raise nextcloud_service.CalendarError("Nextcloud risponde con una pagina web invece di WebDAV: controlla CALDAV_URL, le credenziali e il proxy")
    nextcloud_service.get_calendar_events = calendar_down
    notes_service.list_notes = lambda cat="": []
    news_service.collect_digest = lambda *a, **k: (_ for _ in ()).throw(news_service.NewsError("Nextcloud News non risponde"))
    del sys.modules["database"]                                       # come se il FinanceBot non fosse installato
    sys.modules["database"] = None
    real = monitor_service.MONITOR.card

    def card():
        c = real()
        c["alerts"] = [{"level": "bad", "text": "💿 <b>/mnt/data</b> NON montato: Nextcloud vede una cartella vuota"},
                       {"level": "warn", "text": "🌡️ <b>CPU</b> 82°C da 6 minuti"}]
        c["nextcloud"] = {"ok": False, "why": "timeout", "ms": None}
        c["mounts"][0].update(mounted=False, pct=None)
        c["cpu"]["pct"], c["temp"]["cpu"], c["mem"]["pct"] = 91.0, 82.0, 93.0
        return c
    monitor_service.MONITOR.card = card


def build_demo_app():
    _install_finance_stubs()
    _install_fake_services()
    if os.environ.get("DEMO_FAULTS") == "1":
        _install_faults()
    from services.assistant import Assistant
    from web import cards, security
    from web.app import create_app
    cards.OWNER_ID = 1
    settings = security.WebSettings(allowed_logins=["demo@example.com"], secret=b"demo-secret", assistant_name="EDITH", owner_name="Mario", owner_nick="Mario")
    return _InjectLogin(create_app(settings, Assistant(owner_id=1)))


async def _serve(port):
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(build_demo_app(), host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    print(f"Demo EDITH: apri http://127.0.0.1:{port}   (Ctrl+C per uscire)")
    await server.serve()


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        asyncio.run(_serve(int(os.environ.get("DEMO_PORT", "8788"))))
    except KeyboardInterrupt:
        pass
