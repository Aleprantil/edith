"""
Composizione dei messaggi (HTML di Telegram) per il buongiorno e le news.
Funzioni pure: non fanno rete, così si possono provare facilmente.
"""
import html
import re
from datetime import datetime, time as dtime, timedelta

GIORNI = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
        "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"]

MAX_CHUNK = 3900   # Telegram accetta 4096 caratteri di testo VISIBILE per messaggio (indirizzi dei link e tag non contano)
TITLE_MAX = 140    # un titolo più lungo viene accorciato con «…»

_TAG = re.compile(r"<[^>]+>")


def visible_len(text: str) -> int:
    """Lunghezza come la conta Telegram: senza tag HTML e con le entità (&amp; ecc.) già risolte."""
    return len(html.unescape(_TAG.sub("", text)))

# Nome mostrato per le cartelle di Nextcloud News (chiave: nome della cartella in minuscolo).
# Le cartelle che non sono qui sotto compaiono con il loro nome. Ordine = ordine nel messaggio.
FOLDER_LABELS = {
    "sport": "⚽ Sport",
    "tech": "💻 Tech",
    "italia": "🇮🇹 Italia",
    "mondo": "🌍 Mondo",
    "economia": "💶 Economia",
    "cyber": "🔐 Cyber",
    "wiki": "📚 Wikipedia",
}
FOLDER_ORDER = list(FOLDER_LABELS)


def esc(text) -> str:
    return html.escape(str(text), quote=False)


def it_date(dt: datetime) -> str:
    return f"{GIORNI[dt.weekday()]} {dt.day} {MESI[dt.month - 1]} {dt.year}"


def agenda_section(events, title: str = "Oggi in agenda") -> str:
    """events: lista di dict da get_calendar_events, oppure None se non disponibile."""
    if events is None:
        return f"📅 <b>{esc(title)}</b>\n⚠️ Non riesco a leggere il calendario."
    if not events:
        return f"📅 <b>{esc(title)}</b>\nNessun impegno in programma. 🎉"

    lines = [f"📅 <b>{esc(title)}</b>"]
    for ev in events:
        start, end = ev.get("start"), ev.get("end")
        if isinstance(start, datetime):
            when = start.strftime("%H:%M")
            if isinstance(end, datetime) and end.date() == start.date() and end > start:
                when += "–" + end.strftime("%H:%M")
        else:
            when = "Tutto il giorno"
        loc = f" 📍 {esc(ev['location'])}" if ev.get("location") else ""
        lines.append(f"• <b>{esc(when)}</b> {esc(ev.get('title', 'Senza titolo'))}{loc} <i>({esc(ev.get('calendar', ''))})</i>")
    return "\n".join(lines)


def group_title(g: dict) -> str:
    folder = (g.get("folder") or "").strip()
    if folder:
        return FOLDER_LABELS.get(folder.lower(), f"📰 {folder.capitalize()}")
    return f"📰 {g.get('feed') or 'Notizie'}"


def _group_rank(g: dict) -> int:
    folder = (g.get("folder") or "").lower()
    return FOLDER_ORDER.index(folder) if folder in FOLDER_ORDER else len(FOLDER_ORDER)


def news_sections(groups) -> list:
    sections = []
    for g in sorted(groups, key=_group_rank):
        multi = not g.get("feed")          # più feed nella stessa cartella: mostro la fonte
        lines = [f"<b>{esc(group_title(g))}</b>"]
        for it in g["items"]:
            raw = it.get("line") or it["title"]
            text = esc(raw if len(raw) <= TITLE_MAX else raw[:TITLE_MAX - 1].rstrip() + "…")
            url = it.get("url") or ""
            source = f" <i>· {esc(it['source'])}</i>" if multi and it.get("source") else ""
            if url.startswith(("http://", "https://")):
                lines.append(f'• <a href="{html.escape(url, quote=True)}">{text}</a>{source}')
            else:
                lines.append(f"• {text}{source}")
        sections.append("\n".join(lines))
    return sections


def fmt_duration(minutes: float) -> str:
    m = int(round(minutes))
    h, rest = divmod(m, 60)
    if h and rest:
        return f"{h}h {rest:02d}m"
    return f"{h}h" if h else f"{rest}m"


def weather_line(w: dict, when: str = "Oggi") -> str:
    """Una riga: ⛅ Oggi 12° → 19°, parzialmente nuvoloso · ☔ pioggia 60%. Vuota se manca il meteo."""
    if not w:
        return ""
    line = f"{w['emoji']} <b>{esc(when)}</b> {w['tmin']}° → {w['tmax']}° · {esc(w['label'])}"
    if w.get("pop") is not None and w["pop"] >= 40:
        line += f" · ☔ {w['pop']}%"
    return line


def _timed(events) -> list:
    out = [e for e in (events or []) if isinstance(e.get("start"), datetime)]
    return sorted(out, key=lambda e: e["start"])


def summary_line(events, when: str = "Oggi") -> str:
    """Sintesi a regole fisse (niente AI): 'Oggi: giornata tranquilla, 2 impegni, il primo alle 10:00.'"""
    if events is None:
        return ""
    timed = _timed(events)
    n, all_day = len(timed), len(events) - len(timed)
    mood = "giornata libera" if n == 0 else "giornata tranquilla" if n <= 2 else "giornata nella norma" if n <= 4 else "giornata piena"
    text = f"{when}: {mood}"
    if n:
        text += f", {n} impegn{'o' if n == 1 else 'i'}, il primo alle {timed[0]['start'].strftime('%H:%M')}"
    if all_day:
        text += f" (+{all_day} tutto il giorno)"
    return text + "."


def free_windows(events, day, tz, from_dt: datetime = None, start_h: int = 8, end_h: int = 20,
                 min_minutes: int = 60) -> list:
    """Intervalli liberi di almeno `min_minutes` tra le start_h e le end_h di `day` (gli eventi tutto-il-giorno non contano)."""
    day_start = datetime.combine(day, dtime(start_h), tzinfo=tz)
    day_end = datetime.combine(day, dtime(end_h), tzinfo=tz)
    cursor = max(day_start, from_dt) if from_dt else day_start

    busy = []
    for e in _timed(events):
        end = e["end"] if isinstance(e.get("end"), datetime) and e["end"] > e["start"] else e["start"] + timedelta(hours=1)
        busy.append((e["start"], end))

    windows = []
    for s_, e_ in busy:
        if s_ > cursor:
            windows.append((cursor, min(s_, day_end)))
        cursor = max(cursor, e_)
    if cursor < day_end:
        windows.append((cursor, day_end))
    return [(a, b) for a, b in windows if b > a and (b - a).total_seconds() / 60 >= min_minutes]


def windows_line(windows: list, limit: int = 3) -> str:
    if not windows:
        return ""
    parts = [f"{a.strftime('%H:%M')}–{b.strftime('%H:%M')} ({fmt_duration((b - a).total_seconds() / 60)})" for a, b in windows[:limit]]
    return "🕓 <b>Finestre libere</b>: " + ", ".join(parts)


def _day_overview(events, day, tz, when: str, weather, from_dt=None) -> str:
    """Blocco 'meteo + sintesi + agenda + finestre libere' usato da buongiorno e sera."""
    head = [x for x in (weather_line(weather, when), summary_line(events, when)) if x]
    title = "Oggi in agenda" if when == "Oggi" else "Domani in agenda"
    parts = ["\n".join(head)] if head else []
    parts.append(agenda_section(events, title))
    if events is not None:
        wl = windows_line(free_windows(events, day, tz, from_dt))
        if wl:
            parts[-1] += "\n" + wl
    return "\n\n".join(parts)


def reminder_html(ev: dict, next_ev, minutes: int, after_known: bool = True) -> str:
    """Promemoria prima di un impegno: orario, durata, luogo e cosa c'è dopo."""
    start, end = ev["start"], ev.get("end")
    lines = [f"⏰ <b>Tra circa {minutes} minuti</b>", f"<b>{esc(ev.get('title', 'Senza titolo'))}</b> alle {start.strftime('%H:%M')}"]
    info = []
    if isinstance(end, datetime) and end > start:
        info.append("⏱️ " + fmt_duration((end - start).total_seconds() / 60))
    if ev.get("location"):
        info.append(f"📍 {esc(ev['location'])}")
    info.append(f"📁 <i>{esc(ev.get('calendar', ''))}</i>")
    lines.append(" · ".join(info))

    if after_known:
        after_from = end if isinstance(end, datetime) and end > start else start + timedelta(hours=1)
        if next_ev is None:
            lines.append("Dopo: nessun altro impegno oggi.")
        else:
            gap = (next_ev["start"] - after_from).total_seconds() / 60
            if gap >= 15:
                lines.append(f"Dopo: libero fino alle {next_ev['start'].strftime('%H:%M')} ({fmt_duration(gap)}), poi {esc(next_ev.get('title', ''))}.")
            else:
                lines.append(f"Dopo: subito {esc(next_ev.get('title', ''))} alle {next_ev['start'].strftime('%H:%M')}.")
    return "\n".join(lines)


def tasks_section(tasks, total=None) -> str:
    """«☑️ Promemoria»: i più urgenti (title, label), con «e altri N» se in tutto sono di più. Stringa vuota se non c'è nulla."""
    if not tasks:
        return ""
    lines = ["☑️ <b>Promemoria</b>"]
    for t in tasks:
        lines.append(f"• {esc(t['title'])}" + (f" <i>({esc(t['label'])})</i>" if t.get("label") else ""))
    rest = (total or len(tasks)) - len(tasks)
    if rest > 0:
        lines.append(f"<i>… e altri {rest}</i>")
    return "\n".join(lines)


def build_morning(now: datetime, owner_name: str, events, groups, news_error: str = None, weather: dict = None, tasks=None, tasks_total=None,
                  show_agenda: bool = True, show_news: bool = True) -> list:
    """Sezioni del buongiorno: saluto, meteo, sintesi, agenda e finestre libere; poi le news (o perché mancano).
    show_agenda / show_news: i moduli Calendario e Notizie sono attivi (se no, quelle parti non compaiono)."""
    saluto = f"🌅 <b>Buongiorno{', ' + esc(owner_name) if owner_name else ''}!</b>\n<i>{it_date(now)}</i>"
    if show_agenda:
        sections = [saluto + "\n\n" + _day_overview(events, now.date(), now.tzinfo, "Oggi", weather, from_dt=now)]
    else:
        sections = [saluto + ("\n\n" + weather_line(weather, "Oggi") if weather else "")]
    if tasks_section(tasks, tasks_total):
        sections.append(tasks_section(tasks, tasks_total))
    if not show_news:
        return sections
    if news_error:
        sections.append(f"⚠️ News non disponibili: {esc(news_error)}")
    elif not groups:
        sections.append("📰 Nessuna novità dai tuoi canali.")
    else:
        sections += news_sections(groups)
    return sections


def build_news_update(now: datetime, groups) -> list:
    """Sezioni dell'aggiornamento news di metà giornata; lista vuota se non c'è niente di nuovo."""
    if not groups:
        return []
    sections = news_sections(groups)
    sections[0] = f"🗞️ <b>Aggiornamento news</b> · {now.strftime('%H:%M')}\n\n" + sections[0]
    return sections


def build_evening(now: datetime, tomorrow_events, groups, news_error: str = None, weather: dict = None) -> list:
    """Sera: domani (meteo, sintesi, agenda) + novità dal mezzogiorno. Lista vuota se non c'è niente da dire."""
    sections = []
    if tomorrow_events is None or tomorrow_events:        # None = errore di lettura: lo segnalo
        sections.append(_day_overview(tomorrow_events, (now + timedelta(days=1)).date(), now.tzinfo, "Domani", weather))
    if news_error:
        sections.append(f"⚠️ News non disponibili: {esc(news_error)}")
    elif groups:
        sections += news_sections(groups)
    if sections and weather and not (tomorrow_events is None or tomorrow_events):
        sections.insert(0, weather_line(weather, "Domani"))   # domani libero ma c'è altro da dire: aggiungo il meteo
    if not sections:
        return []
    sections[0] = f"🌙 <b>Buonasera!</b> · {now.strftime('%H:%M')}\n\n" + sections[0]
    return sections


def build_latest(groups, chosen=None, topic: str = "") -> list:
    """«Ultime notizie»: tutte le cartelle, oppure quella chiesta. Lista vuota se non c'è niente."""
    if not groups:
        return []
    if chosen:
        titolo = FOLDER_LABELS.get(chosen[0].lower(), f"📰 {chosen[0].capitalize()}")
        head = f"🗞️ <b>Ultime notizie</b> · {esc(titolo)}"
        # il titolo della cartella è già nell'intestazione: nelle sezioni non lo ripeto
        sections = []
        for g in groups:
            sec = news_sections([g])[0].split("\n", 1)
            sections.append(sec[1] if len(sec) > 1 else "")
        sections = [x for x in sections if x]
        if not sections:
            return []
        sections[0] = head + "\n" + sections[0]
        return sections
    sections = news_sections(groups)
    sections[0] = "🗞️ <b>Ultime notizie</b>\n\n" + sections[0]
    return sections


NEWS_STALE_HOURS = 6    # se il più recente di tutti i feed è più vecchio di così, probabilmente News non si aggiorna
CRON_HINT = ("In Nextcloud: Impostazioni di amministrazione → Impostazioni di base → «Processi in background»: "
             "deve essere «Cron» e «Ultima esecuzione» di pochi minuti fa.")


def _ago(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 3600:
        return f"{max(1, s // 60)} min fa"
    if s < 86400:
        return f"{s // 3600} h fa"
    return f"{s // 86400} giorni fa"


def _newest(rows):
    return max((r["newest_pub"] for r in rows if r.get("newest_pub")), default=None)


def feeds_status_text(rows, now: float) -> str:
    """Il comando /newsstato: per ogni feed l'ultimo articolo, quanti da inviare, errori."""
    if not rows:
        return "📡 In News non ho trovato nessun feed."
    lines = ["📡 <b>Stato dei feed in News</b>"]
    order = sorted(rows, key=lambda r: (_group_rank({"folder": r.get("folder", "")}), r["feed"].lower()))
    for r in order:
        age = f"ultimo articolo {_ago(now - r['newest_pub'])}" if r.get("newest_pub") else "nessun articolo"
        extra = f" · {r['new']} da inviare" if r.get("new") else ""
        err = ""
        if r.get("error_count") or r.get("error"):
            err = f" · ⚠️ {r.get('error_count') or '?'} errori" + (f": {esc(r['error'][:70])}" if r.get("error") else "")
        folder = f" <i>({esc(r['folder'])})</i>" if r.get("folder") else ""
        lines.append(f"• {esc(r['feed'])}{folder}: {age}{extra}{err}")
    newest = _newest(rows)
    if newest is None:
        lines.append("\n⚠️ Nessun feed ha ancora articoli: News non li ha scaricati. " + CRON_HINT)
    elif now - newest > NEWS_STALE_HOURS * 3600:
        lines.append(f"\n⚠️ Il più recente di tutti è di {_ago(now - newest)}: con fonti che pubblicano di continuo "
                     "vuol dire che News non si sta aggiornando. " + CRON_HINT)
    else:
        lines.append("\n✅ I feed si stanno aggiornando.")
    return "\n".join(lines)


def no_news_text(rows, now: float) -> str:
    """Risposta a «nessuna novità»: dice anche SE i feed sono fermi o se le notizie erano già state inviate."""
    base = "📰 Nessuna novità dall'ultimo riepilogo."
    newest = _newest(rows or [])
    if newest is None:
        return base + "\n⚠️ In News non vedo articoli: " + CRON_HINT
    line = f"\nL'articolo più recente in News è di {_ago(now - newest)}."
    if now - newest > NEWS_STALE_HOURS * 3600:
        return base + line + "\n⚠️ Sembra che News non si stia aggiornando. " + CRON_HINT + " Dettagli: /newsstato"
    return base + line + " Sono già state inviate. Dettagli: /newsstato"


def chunk_sections(sections, limit: int = MAX_CHUNK) -> list:
    """Unisce le sezioni in messaggi ≤ limit caratteri visibili, senza spezzare una sezione a metà."""
    chunks, current = [], ""
    for sec in sections:
        if visible_len(sec) > limit:               # sezione enorme: la taglio per righe
            buf = ""
            for line in sec.split("\n"):
                if buf and visible_len(buf) + visible_len(line) + 1 > limit:
                    chunks.append(buf)
                    buf = ""
                buf = f"{buf}\n{line}" if buf else line
            sec = buf
        if current and visible_len(current) + visible_len(sec) + 2 > limit:
            chunks.append(current)
            current = sec
        else:
            current = f"{current}\n\n{sec}" if current else sec
    if current:
        chunks.append(current)
    return chunks
