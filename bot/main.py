import asyncio
import logging
import time
from datetime import datetime, timedelta, time as dtime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from telegram import LinkPreviewOptions, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    ConversationHandler, Defaults, MessageHandler, filters,
)

from config import (
    ASSISTANT_NAME, CALENDAR_USER_IDS, CHARTS_ENABLED, CHARTS_TIME, ENGINE_UNIFIED, FINANCE_USER_IDS, MON_ENABLED, MON_INTERVAL, MORNING_TIME, NEWS_EVENING_TIME, NEWS_TIME, OWNER_NAME,
    WEB_ENABLED,
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, WEATHER_PLACE,
)
try:
    from bot.engine_bridge import EngineBridge, features_for_ids
except ImportError:                                  # avviato come script dalla cartella bot/
    from engine_bridge import EngineBridge, features_for_ids
from services.gemini_service import parse_user_input
from services.nextcloud_service import (
    CalendarError,
    create_calendar_event,
    delete_calendar_event,
    get_calendar_events,
    get_free_slots,
    prewarm,
    update_calendar_event,
)
from services.notes_service import NotesError, get_note_content, list_notes, save_note_to_nextcloud
from services import charts_export, digest, memory_service, monitor_service, news_service, plugins, tasks_service, weather_service
from services.news_service import NewsError

# --- Modulo Finanze: il suo codice (cartella finance/) si carica SOLO se il modulo è attivo ---
FINANCE = False
if plugins.on("finance"):
    try:
        import database as fin_db
        import scheduler as fin_scheduler
        from handlers import (
            SPESA_CAT, SPESA_IMP, SPESA_NOTA, ENTRATA_TIPO, ENTRATA_IMP, ENTRATA_NOTA, BUDGET_CAT, BUDGET_IMP,
            spesa_start, spesa_categoria, spesa_importo, spesa_nota, spesa_nota_skip,
            entrata_start, entrata_tipo, entrata_importo, entrata_nota, entrata_nota_skip,
            riepilogo_giornaliero, riepilogo_settimanale, riepilogo_mensile, riepilogo_annuale,
            grafico_categorie, grafico_trend, budget_start, budget_categoria, budget_importo, budget_view,
            cerca_transazione, elimina_ultima, esporta_csv, obiettivi_start, obiettivi_menu, obiettivi_input,
            cancel as fin_cancel,
        )
        from services import finance_nl
        FINANCE = True
    except ImportError as e:
        logging.getLogger(__name__).error("Modulo Finanze attivo, ma il suo codice (cartella finance/) manca: lo spengo (%s)", e)
        plugins.set_enabled([n for n in plugins.enabled() if n != "finance"])

# Configurazione logging
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
# httpx a livello INFO stampa ogni richiesta a Telegram, token del bot compreso
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

TZ_ROMA = ZoneInfo("Europe/Rome")
NEUTRAL_CATEGORIES = ("generale", "lavoro", "svago")
MAX_MESSAGE_LEN = 4000  # Telegram accetta max 4096 caratteri


# ---------------------------------------------------------------------------
# Utilità
# ---------------------------------------------------------------------------

def parse_dt(value) -> datetime | None:
    """Stringa ISO -> datetime con fuso (Europe/Rome se mancante). None se non valida."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=TZ_ROMA)


async def _send(fn, text: str, markdown: bool, **kwargs):
    """Invia un messaggio; se il Markdown è malformato (titoli con _ o *) riprova come testo semplice."""
    if len(text) > MAX_MESSAGE_LEN:
        text = text[: MAX_MESSAGE_LEN - 1] + "…"
    if markdown:
        try:
            return await fn(text=text, parse_mode="Markdown", **kwargs)
        except BadRequest as e:
            logger.warning("Markdown non valido (%s), reinvio come testo semplice", e)
            text = text.replace("*", "").replace("_", "").replace("`", "")
    return await fn(text=text, **kwargs)


async def reply(update: Update, text: str, markdown: bool = False):
    return await _send(update.message.reply_text, text, markdown)


def _allowed(update: Update, ids: set) -> bool:
    if not ids:          # nessun limite configurato (segnalato nei log all'avvio)
        return True
    user = update.effective_user
    return user is not None and user.id in ids


def can_use_calendar(update: Update) -> bool:
    return _allowed(update, CALENDAR_USER_IDS)


def can_use_finance(update: Update) -> bool:
    return _allowed(update, FINANCE_USER_IDS)


async def deny(update: Update, detail: str = ""):
    user = update.effective_user
    logger.warning("Accesso negato all'utente %s", user.id if user else "?")
    if update.message:
        await update.message.reply_text(f"⛔ Non sei autorizzato{detail}.")


async def guard(update: Update) -> bool:
    """Accesso a calendario e note."""
    if can_use_calendar(update):
        return True
    await deny(update, " a usare il calendario")
    return False


async def safe(update: Update, coro):
    """Esegue una coroutine e trasforma gli errori in un messaggio comprensibile."""
    try:
        await coro
    except (CalendarError, NotesError, NewsError) as e:
        logger.warning("%s", e)
        await reply(update, f"❌ {e}")
    except Exception:
        logger.exception("Errore non gestito")
        await reply(update, "❌ Errore inatteso, controlla i log del container.")


# ---------------------------------------------------------------------------
# Promemoria in background
# ---------------------------------------------------------------------------

async def check_upcoming_events(context: ContextTypes.DEFAULT_TYPE):
    """Ogni 5 minuti avvisa degli eventi che iniziano tra 25 e 35 minuti (una sola volta per evento)."""
    if not TELEGRAM_CHAT_ID:
        return

    now = datetime.now(TZ_ROMA)
    win_start = now + timedelta(minutes=25)
    win_end = now + timedelta(minutes=35)

    try:
        events = await asyncio.to_thread(get_calendar_events, win_start, win_end)
    except CalendarError as e:
        logger.warning("Promemoria saltato: %s", e)
        return

    notified = context.bot_data.setdefault("notified", {})
    for key in [k for k, ts in notified.items() if ts < now - timedelta(hours=2)]:
        del notified[key]

    for ev in events:
        start = ev.get("start")
        # Solo eventi che INIZIANO nella finestra (la ricerca restituisce anche quelli già in corso)
        if not isinstance(start, datetime) or not (win_start <= start <= win_end):
            continue

        titolo = ev.get("title", "Senza titolo")
        cal_nome = ev.get("calendar", "generale")
        key = (titolo, cal_nome, start.isoformat())
        if key in notified:
            continue
        notified[key] = now

        # Cosa c'è dopo (una ricerca in più, solo quando c'è davvero un promemoria da mandare)
        end_dt = ev.get("end") if isinstance(ev.get("end"), datetime) and ev["end"] > start else start + timedelta(hours=1)
        next_ev, after_known = None, True
        try:
            later = await asyncio.to_thread(
                get_calendar_events, end_dt, datetime.combine(start.date(), dtime.max, tzinfo=TZ_ROMA))
            next_ev = next((e for e in later if isinstance(e.get("start"), datetime) and e["start"] >= end_dt), None)
        except CalendarError:
            after_known = False

        minutes = max(5, int(round((start - now).total_seconds() / 60 / 5)) * 5)   # "circa 30", non "28"
        try:
            await _send_chunks(context.bot, TELEGRAM_CHAT_ID, [digest.reminder_html(ev, next_ev, minutes, after_known)])
            logger.info("Promemoria inviato per: %s", titolo)
        except Exception as e:
            logger.error("Errore nell'invio del promemoria Telegram: %s", e)


# ---------------------------------------------------------------------------
# Buongiorno e news
# ---------------------------------------------------------------------------

async def _send_chunks(bot, chat_id, chunks: list):
    for chunk in chunks:
        await bot.send_message(
            chat_id=chat_id, text=chunk, parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True),   # 25 link = 25 anteprime: no
        )


def _pick_day(forecast, index: int):
    """Giorno `index` (0 oggi, 1 domani) dalle previsioni, oppure None se il meteo non è disponibile."""
    if isinstance(forecast, Exception):
        logger.warning("Meteo non disponibile: %s", forecast)
        return None
    return forecast[index] if len(forecast) > index else None


async def send_morning(bot, chat_id):
    """Buongiorno: agenda di oggi + notizie nuove dei canali. Se una delle due parti non funziona, manda l'altra."""
    now = datetime.now(TZ_ROMA)
    day_start = datetime.combine(now.date(), dtime.min, tzinfo=TZ_ROMA)
    day_end = datetime.combine(now.date(), dtime.max, tzinfo=TZ_ROMA)

    t0 = time.perf_counter()

    async def skip(value=None):                            # modulo spento: nessuna richiesta
        return value
    agenda, news, forecast, todos = await asyncio.gather(
        asyncio.to_thread(get_calendar_events, day_start, day_end) if plugins.on("calendar") else skip([]),
        asyncio.to_thread(news_service.collect_digest) if plugins.on("news") else skip([]),
        asyncio.to_thread(weather_service.get_forecast) if plugins.on("weather") else skip([]),
        asyncio.to_thread(tasks_service.open_tasks) if plugins.on("tasks") else skip(None),
        return_exceptions=True,
    )
    logger.info("[timing] buongiorno: dati raccolti in %.1fs", time.perf_counter() - t0)
    weather = _pick_day(forecast, 0)

    events = None if isinstance(agenda, Exception) else agenda
    if isinstance(agenda, Exception):
        logger.warning("Buongiorno: agenda non disponibile: %s", agenda)

    groups, news_error = None, None
    if isinstance(news, Exception):
        logger.warning("Buongiorno: news non disponibili: %s", news)
        news_error = str(news) if isinstance(news, NewsError) else "errore imprevisto (vedi log)"
    else:
        groups = news

    tasks, tasks_total = None, None
    if todos is None:
        pass
    elif isinstance(todos, Exception):
        logger.warning("Buongiorno: promemoria non disponibili: %s", todos)
    else:
        tasks, tasks_total = tasks_service.top(tasks_service.public(todos, now))             # i 7 più urgenti della lista TO DO

    sections = digest.build_morning(now, OWNER_NAME, events, groups, news_error, weather, tasks=tasks, tasks_total=tasks_total,
                                    show_agenda=plugins.on("calendar"), show_news=plugins.on("news"))
    await _send_chunks(bot, chat_id, digest.chunk_sections(sections))
    if groups:
        await asyncio.to_thread(news_service.commit, groups)   # le segno inviate solo dopo l'invio riuscito


async def send_news_update(bot, chat_id, manual: bool = False):
    """Novità arrivate dall'ultimo invio. A mezzogiorno, se non c'è niente di nuovo, non scrive."""
    now = datetime.now(TZ_ROMA)
    groups = await asyncio.to_thread(news_service.collect_digest)
    sections = digest.build_news_update(now, groups)
    if not sections:
        if manual:
            text = "📰 Nessuna novità dall'ultimo riepilogo."
            try:   # dico anche PERCHÉ: feed fermi, oppure notizie già inviate
                rows = await asyncio.to_thread(news_service.feed_status)
                text = digest.no_news_text(rows, time.time())
            except Exception as e:
                logger.warning("Stato dei feed non disponibile: %s", e)
            await _send_chunks(bot, chat_id, [text])
        return
    await _send_chunks(bot, chat_id, digest.chunk_sections(sections))
    await asyncio.to_thread(news_service.commit, groups)


async def send_evening(bot, chat_id, manual: bool = False):
    """Sera: novità dal mezzogiorno + agenda di domani. Se non c'è niente da dire, non scrive."""
    now = datetime.now(TZ_ROMA)
    tomorrow = (now + timedelta(days=1)).date()
    day_start = datetime.combine(tomorrow, dtime.min, tzinfo=TZ_ROMA)
    day_end = datetime.combine(tomorrow, dtime.max, tzinfo=TZ_ROMA)

    agenda, news, forecast = await asyncio.gather(
        asyncio.to_thread(get_calendar_events, day_start, day_end),
        asyncio.to_thread(news_service.collect_digest),
        asyncio.to_thread(weather_service.get_forecast),
        return_exceptions=True,
    )
    weather = _pick_day(forecast, 1)
    events = None if isinstance(agenda, Exception) else agenda
    if isinstance(agenda, Exception):
        logger.warning("Sera: agenda non disponibile: %s", agenda)

    groups, news_error = None, None
    if isinstance(news, Exception):
        logger.warning("Sera: news non disponibili: %s", news)
        news_error = str(news) if isinstance(news, NewsError) else "errore imprevisto (vedi log)"
    else:
        groups = news

    sections = digest.build_evening(now, events, groups, news_error, weather)
    if not sections:
        if manual:
            await bot.send_message(chat_id=chat_id, text="🌙 Niente di nuovo: nessuna notizia e nessun impegno domani.")
        return
    await _send_chunks(bot, chat_id, digest.chunk_sections(sections))
    if groups:
        await asyncio.to_thread(news_service.commit, groups)


def _owner_uid() -> int:
    """L'utente delle finanze: il tuo id Telegram (in chat privata è lo stesso della chat)."""
    chat = str(TELEGRAM_CHAT_ID or "")
    if chat.lstrip("-").isdigit():
        return int(chat)
    return sorted(FINANCE_USER_IDS)[0] if FINANCE_USER_IDS else 0


async def job_charts(context: ContextTypes.DEFAULT_TYPE):
    """Ogni notte: aggiorna i grafici in Nextcloud. Se riesce non scrive; se non riesce te lo dice."""
    try:
        report = await asyncio.to_thread(charts_export.run, _owner_uid())
        logger.info("[OK] Grafici aggiornati: %s", charts_export.describe(report).replace("\n", " | "))
    except Exception as e:
        logger.exception("Grafici: aggiornamento notturno fallito")
        msg = str(e) if isinstance(e, charts_export.ChartsError) else f"errore imprevisto ({e.__class__.__name__}, vedi i log)"
        await _send_chunks(context.bot, TELEGRAM_CHAT_ID, [f"📊 Non sono riuscito ad aggiornare i grafici in Nextcloud: {msg}"])


async def cmd_grafici(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Aggiorna subito i grafici salvati in Nextcloud (di notte lo fa da solo)."""
    if not await guard(update):
        return
    await update.message.reply_text("📊 Aggiorno i grafici…")
    try:
        report = await asyncio.to_thread(charts_export.run, update.effective_user.id)
        await update.message.reply_text(charts_export.describe(report))
    except charts_export.ChartsError as e:
        await update.message.reply_text(f"❌ {e}")
    except Exception as e:
        logger.exception("Grafici: errore")
        await update.message.reply_text(f"❌ Errore imprevisto ({e.__class__.__name__}): vedi i log.")


async def job_morning(context: ContextTypes.DEFAULT_TYPE):
    try:
        await send_morning(context.bot, TELEGRAM_CHAT_ID)
    except Exception:
        logger.exception("Buongiorno automatico fallito")


async def job_news(context: ContextTypes.DEFAULT_TYPE):
    try:
        await send_news_update(context.bot, TELEGRAM_CHAT_ID)
    except NewsError as e:
        logger.warning("Riepilogo news saltato: %s", e)
    except Exception:
        logger.exception("Riepilogo news automatico fallito")


async def job_evening(context: ContextTypes.DEFAULT_TYPE):
    try:
        await send_evening(context.bot, TELEGRAM_CHAT_ID)
    except Exception:
        logger.exception("Aggiornamento serale fallito")


async def cmd_sera(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    await safe(update, send_evening(context.bot, update.effective_chat.id, manual=True))


async def cmd_meteo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    try:
        days = await asyncio.to_thread(weather_service.get_forecast)
    except weather_service.WeatherError as e:
        await reply(update, f"❌ Meteo non disponibile: {e}")
        return
    lines = [f"<b>Meteo · {digest.esc(WEATHER_PLACE)}</b>"]
    for day, label in zip(days, ("Oggi", "Domani")):
        lines.append(digest.weather_line(day, label))
    await _send_chunks(context.bot, update.effective_chat.id, ["\n".join(lines)])


# ---------------------------------------------------------------------------
# Monitoraggio del server
# ---------------------------------------------------------------------------

async def job_monitor(context: ContextTypes.DEFAULT_TYPE):
    """Ogni minuto: legge lo stato del server e manda avvisi / riepilogo dell'avvio solo se serve."""
    try:
        messages = await asyncio.to_thread(monitor_service.get_monitor().tick)
    except Exception:
        logger.exception("Monitor: controllo fallito")
        return
    for text in messages:
        try:
            await _send_chunks(context.bot, TELEGRAM_CHAT_ID, digest.chunk_sections([text]))
        except Exception as e:
            logger.error("Monitor: invio fallito: %s", e)


async def _send_server_status(bot, chat_id):
    text = await asyncio.to_thread(monitor_service.get_monitor().server_report)
    await _send_chunks(bot, chat_id, digest.chunk_sections([text]))


async def cmd_server(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    await safe(update, _send_server_status(context.bot, update.effective_chat.id))


async def cmd_boot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Riepilogo dell'ultimo riavvio: cosa è successo prima e come è partito il server."""
    if not await guard(update):
        return
    text = await asyncio.to_thread(monitor_service.get_monitor().boot_report_text)
    await _send_chunks(context.bot, update.effective_chat.id, digest.chunk_sections([text]))


async def cmd_silenzia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/silenzia [ore]: sospende gli avvisi del server (manutenzione). /silenzia 0 li riattiva."""
    if not await guard(update):
        return
    try:
        hours = float(context.args[0].replace(",", ".")) if context.args else 2.0
    except ValueError:
        await reply(update, "Uso: /silenzia [ore]  (es. /silenzia 2, oppure /silenzia 0 per riattivare)")
        return
    hours = min(max(hours, 0.0), 72.0)
    await _send_chunks(context.bot, update.effective_chat.id, [monitor_service.get_monitor().mute(hours)])


async def cmd_sveglia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    await safe(update, send_morning(context.bot, update.effective_chat.id))


async def cmd_newsstato(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Per ogni feed: ultimo articolo, quanti da inviare, errori. Serve a capire se News si sta aggiornando."""
    if not await guard(update):
        return
    async def _go():
        rows = await asyncio.to_thread(news_service.feed_status)
        await _send_chunks(context.bot, update.effective_chat.id, digest.chunk_sections([digest.feeds_status_text(rows, time.time())]))
    await safe(update, _go())


async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    await safe(update, send_news_update(context.bot, update.effective_chat.id, manual=True))


# ---------------------------------------------------------------------------
# Comandi rapidi
# ---------------------------------------------------------------------------

async def process_and_send_events(update: Update, start_dt: datetime, end_dt: datetime, label: str):
    events = await asyncio.to_thread(get_calendar_events, start_dt, end_dt)

    if not events:
        await reply(update, f"📅 Non hai nessun impegno in programma {label}.")
        return

    risposta = f"📅 *I tuoi impegni {label}:*\n"
    for ev in events:
        titolo = ev.get("title", "Senza titolo")
        start = ev.get("start")
        end = ev.get("end")
        cal_nome = ev.get("calendar", "generale")

        # Evento a "tutto il giorno": oggetti date anziché datetime
        if not isinstance(start, datetime) and hasattr(start, "strftime"):
            if end and hasattr(end, "strftime"):
                end_reale = end - timedelta(days=1)
                if start == end_reale:
                    orario_str = "📅 Tutto il giorno"
                    data_str = f" ({start.strftime('%d/%m')})"
                else:
                    orario_str = "📅 Più giorni"
                    data_str = f" ({start.strftime('%d/%m')} al {end_reale.strftime('%d/%m')})"
            else:
                orario_str = "📅 Tutto il giorno"
                data_str = f" ({start.strftime('%d/%m')})"
        elif isinstance(start, datetime):
            orario_str = start.strftime("%H:%M")
            data_str = ""
        else:
            orario_str = "Orario non specificato"
            data_str = ""

        risposta += f"- *{orario_str}*{data_str} | 📁 _{cal_nome}_: {titolo}\n"

    await reply(update, risposta, markdown=True)


async def cmd_oggi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    today = datetime.now(TZ_ROMA).date()
    start = datetime.combine(today, dtime.min, tzinfo=TZ_ROMA)
    end = datetime.combine(today, dtime.max, tzinfo=TZ_ROMA)
    await safe(update, process_and_send_events(update, start, end, "di oggi"))


async def cmd_domani(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    tomorrow = (datetime.now(TZ_ROMA) + timedelta(days=1)).date()
    start = datetime.combine(tomorrow, dtime.min, tzinfo=TZ_ROMA)
    end = datetime.combine(tomorrow, dtime.max, tzinfo=TZ_ROMA)
    await safe(update, process_and_send_events(update, start, end, "di domani"))


HELP_CALENDAR = (
    "📅 *Calendario e note* (scrivimi in italiano)\n"
    "  «segna domani alle 8 colazione al bar»\n"
    "  «cosa faccio giovedì?» · «quando sono libero domani?»\n"
    "  «sposta la colazione di domani alle 10»\n"
    "  «prendi nota: latte, uova» · «leggimi la nota spesa»\n"
    "  /oggi — impegni di oggi   /domani — impegni di domani\n"
    "  /sveglia (o /briefing) — buongiorno con meteo, agenda e news   /news — novità dai tuoi canali   /newsstato — se i feed si aggiornano\n"
    "  /meteo — oggi e domani\n"
    "🖥️ *Server*: /server (stato completo, o «come sta il server?») · /boot (cosa è successo al riavvio) · /silenzia 2 (sospende gli avvisi per 2 ore)\n"
    "  /sera — news della sera con l'agenda di domani\n"
    "  oppure a parole: «ultime news sul calcio», «novità di tecnologia», «news generali»\n"
    "☑️ *Promemoria*: «aggiungimi un promemoria domani: da pagina 20 a 30» · «spunta promemoria da pagina 20 a 30» · «che promemoria ho?»\n"
    "🧠 *Memoria*: «ricordati che il calcio bambini è il martedì» · «cosa ricordi di me?» · «dimentica …»\n"
    "📊 /grafici — aggiorna i grafici salvati in Nextcloud\n"
    "  Per cancellare o spostare ti chiedo conferma con i pulsanti.\n"
)
HELP_FINANCE = (
    "💰 *Finanze* (a parole o con i comandi)\n"
    "  «ho speso 12 euro di pizza» · «ieri 30 euro di benzina»\n"
    "  «ho ricevuto lo stipendio 1500»\n"
    "  «quanto ho speso questo mese?» · «quanto in ristoranti quest'anno?»\n"
    "  /spesa  /entrata  /elimina — registra o cancella l'ultima\n"
    "  /riepilogo (oggi)  /settimana  /mese  /anno\n"
    "  /grafico  /trend  /budget  /budgetview  /obiettivi  /cerca  /esporta\n"
    "  /annulla — annulla l'operazione in corso\n"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cal, fin = can_use_calendar(update), can_use_finance(update)
    if not (cal or fin):
        await deny(update)
        return
    if fin and FINANCE and update.effective_user:
        fin_db.get_settings(update.effective_user.id)   # crea le impostazioni al primo accesso
    name = update.effective_user.first_name if update.effective_user else ""
    parts = [f"👋 Ciao *{name}*! Sono {ASSISTANT_NAME}.\n"]
    if cal:
        parts.append(HELP_CALENDAR)
    if fin and FINANCE:
        parts.append(HELP_FINANCE)
    parts.append("🧩 Moduli attivi: " + ", ".join(plugins.CATALOG[n].title for n in plugins.enabled()))
    await reply(update, "\n".join(parts), markdown=True)


async def cmd_annulla(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fuori da una conversazione: azzera anche l'inserimento guidato degli obiettivi."""
    context.user_data.pop("obj_step", None)
    await update.message.reply_text("❌ Operazione annullata.")


# ---------------------------------------------------------------------------
# Handler per intent
# ---------------------------------------------------------------------------

def extract_fields(parsed: dict) -> SimpleNamespace:
    return SimpleNamespace(
        intent=parsed.get("intent"),
        title=str(parsed.get("title") or "").strip(),
        start_str=parsed.get("start_time"),
        end_str=parsed.get("end_time"),
        category=parsed.get("category") or "svago",
        raw_category=parsed.get("category") or "",
        description=parsed.get("description") or "",
        location=parsed.get("location") or "",
        rrule=parsed.get("rrule"),
        amount=parsed.get("amount"),
        finance_category=parsed.get("finance_category"),
        period=parsed.get("period"),
        news_topic=parsed.get("news_topic"),
        explicit_original=bool(parsed.get("original_time")),
        # original_time serve per modifiche e cancellazioni; se manca, fallback su start_time
        original_str=parsed.get("original_time") or parsed.get("start_time"),
        parsed=parsed,
    )


async def handle_delete(update: Update, f: SimpleNamespace, user_text: str):
    if not f.title:
        await reply(update, "❌ Dimmi quale evento vuoi cancellare.")
        return

    target = parse_dt(f.original_str) or datetime.combine(
        datetime.now(TZ_ROMA).date(), dtime.min, tzinfo=TZ_ROMA
    )
    # Senza una data precisa cerco nelle prossime due settimane (una sola ricerca)
    scan_days = 1 if f.explicit_original else 14

    deleted = await asyncio.to_thread(delete_calendar_event, f.category, target, f.title, scan_days)
    if deleted:
        start = deleted.get("start")
        giorno = start.strftime("%d/%m/%Y") if hasattr(start, "strftime") else target.strftime("%d/%m/%Y")
        await reply(update, f"🗑️ Evento *'{f.title}'* cancellato correttamente per il giorno {giorno}.", True)
    else:
        await reply(update, f"❌ Non ho trovato nessun evento chiamato '{f.title}' da cancellare.", True)


async def handle_update(update: Update, f: SimpleNamespace, user_text: str):
    orig_dt = parse_dt(f.original_str)
    new_start = parse_dt(f.start_str)
    if not orig_dt or not new_start:
        await reply(update, "❌ Non ho capito quale evento spostare e a quando. Riprova indicando giorno e nuovo orario.")
        return
    new_end = parse_dt(f.end_str)

    ok = await asyncio.to_thread(update_calendar_event, f.category, orig_dt, new_start, new_end, f.title)
    if ok:
        await reply(
            update,
            f"🔄 Evento *{f.title}* spostato con successo a:\n📅 {new_start.strftime('%d/%m/%Y %H:%M')}",
            True,
        )
    else:
        await reply(update, f"❌ Impossibile spostare. Evento '{f.title}' non trovato.")


async def handle_free_slots(update: Update, f: SimpleNamespace, user_text: str):
    slot_start = parse_dt(f.start_str)
    slot_end = parse_dt(f.end_str)

    # L'utente ha specificato una fascia precisa (es. "dalle 17 alle 18")
    if slot_start and slot_end:
        conflicts = await asyncio.to_thread(get_calendar_events, slot_start, slot_end)

        overlapping = []
        for ev in conflicts:
            ev_s = ev.get("start")
            ev_e = ev.get("end") or (ev_s + timedelta(hours=1) if isinstance(ev_s, datetime) else None)
            if isinstance(ev_s, datetime) and isinstance(ev_e, datetime):
                if max(slot_start, ev_s) < min(slot_end, ev_e):
                    overlapping.append(ev)

        if not overlapping:
            await reply(
                update,
                f"✅ Sì, sei completamente libero dalle {slot_start.strftime('%H:%M')} alle {slot_end.strftime('%H:%M')}!",
            )
        else:
            msg = "❌ No, in quella fascia hai già:\n"
            for ev in overlapping:
                s_str = ev["start"].strftime("%H:%M") if isinstance(ev["start"], datetime) else ""
                msg += f"- *{ev['title']}* (📁 {ev['calendar']}) dalle {s_str}\n"
            await reply(update, msg, True)
        return

    # Tempo libero generico del giorno
    search_date = slot_start or datetime.now(TZ_ROMA)
    slots = await asyncio.to_thread(get_free_slots, search_date, f.category)
    if not slots:
        await reply(update, "Non hai tempo libero in quella data! 🥵")
    else:
        msg = "✅ Ecco i tuoi momenti liberi:\n"
        for s_slot, e_slot in slots:
            msg += f"- Dalle {s_slot.strftime('%H:%M')} alle {e_slot.strftime('%H:%M')}\n"
        await reply(update, msg)


async def handle_create(update: Update, f: SimpleNamespace, user_text: str):
    start_dt = parse_dt(f.start_str)
    if not start_dt:
        await reply(update, "❌ Non ho capito quando fissare l'evento. Indica giorno e ora.")
        return

    title = f.title or "Senza titolo"
    result = await asyncio.to_thread(
        create_calendar_event,
        title=title,
        start_time_str=f.start_str,
        description=f.description,
        calendar_name=f.category,
        rrule=f.rrule,
        end_time_str=f.end_str,
        location=f.location,
    )

    if result.get("success") is True:
        cal_usato = result.get("calendar_used", f.category)
        rip_info = f" (Ricorrente: {f.rrule})" if f.rrule else ""
        luogo_info = f"\n📍 Luogo: {f.location}" if f.location else ""
        msg = (
            f"✅ Evento salvato nel calendario *{cal_usato}*{rip_info}:\n"
            f"*{title}*{luogo_info}\n📅 {start_dt.strftime('%d/%m/%Y %H:%M')}"
        )
        if result.get("conflict"):
            msg += "\n\n⚠️ *ATTENZIONE*: Hai già un altro impegno segnato per questo orario!"
        await reply(update, msg, True)
    else:
        await reply(update, f"❌ Errore nel salvataggio su Nextcloud: {result.get('message')}")


async def handle_get_events(update: Update, f: SimpleNamespace, user_text: str):
    now = datetime.now(TZ_ROMA)
    start_dt = parse_dt(f.start_str) or datetime.combine(now.date(), dtime.min, tzinfo=TZ_ROMA)
    end_dt = parse_dt(f.end_str)
    if end_dt is None:
        is_topic_query = bool(f.title or (f.category and f.category.lower() not in NEUTRAL_CATEGORIES))
        if is_topic_query and not f.start_str:
            end_dt = start_dt + timedelta(days=7)
        else:
            end_dt = datetime.combine(start_dt.date(), dtime.max, tzinfo=TZ_ROMA)

    events = await asyncio.to_thread(get_calendar_events, start_dt, end_dt)

    filter_term = f.title.lower()
    cat_term = (f.category or "").lower().strip()
    has_cat_filter = bool(cat_term) and cat_term not in NEUTRAL_CATEGORIES

    filtered = []
    for ev in events:
        ev_title = str(ev.get("title", "")).lower()
        ev_cal = str(ev.get("calendar", "")).lower()
        match_term = not filter_term or (filter_term in ev_title or filter_term in ev_cal)
        match_cat = not has_cat_filter or (cat_term in ev_cal or cat_term in ev_title)
        if match_term and match_cat:
            filtered.append(ev)

    events_to_show = filtered if (filter_term or has_cat_filter) else events

    if not events_to_show:
        await reply(update, f"📅 Non ho trovato impegni per la richiesta: _{user_text}_", True)
        return

    risposta = f"📅 *Risultati per: {user_text}*\n"
    for ev in events_to_show:
        orario = ev.get("start")
        loc = ev.get("location")
        loc_str = f" 📍 _{loc}_" if loc else ""
        orario_str = orario.strftime("%d/%m %H:%M") if isinstance(orario, datetime) else "Tutto il giorno"
        risposta += f"- *{orario_str}* | 📁 _{ev.get('calendar', 'generale')}_: {ev.get('title', 'Senza titolo')}{loc_str}\n"

    await reply(update, risposta, True)


async def handle_add_note(update: Update, f: SimpleNamespace, user_text: str):
    note_title = f.title if f.title and f.title != "Senza titolo" else "Nota"
    note_content = f.description if f.description else user_text

    result = await asyncio.to_thread(save_note_to_nextcloud, note_title, note_content, f.raw_category)

    if result.get("success"):
        await reply(
            update,
            f"📝 *Nota salvata su Nextcloud!*\n\n"
            f"📌 Titolo: *{result['title']}*\n"
            f"📁 Cartella: _{result.get('folder', 'Note')}_\n\n"
            f"{note_content}",
            True,
        )
    else:
        await reply(update, f"❌ Errore durante il salvataggio della nota: {result.get('error')}")


async def handle_get_notes(update: Update, f: SimpleNamespace, user_text: str):
    if f.title:
        content = await asyncio.to_thread(get_note_content, f.title, f.raw_category)
        if content:
            await reply(update, f"📄 Nota trovata:\n\n{content}")  # testo semplice: il contenuto è Markdown libero
        else:
            await reply(update, f"❌ Nota '{f.title}' non trovata.")
        return

    notes = await asyncio.to_thread(list_notes, f.raw_category)
    if not notes:
        await reply(update, "📭 Nessuna nota trovata.")
        return

    msg = "📝 *Le tue note:*\n"
    for n in notes:
        msg += f"- `{n['title']}`\n"
    msg += "\n_Puoi chiedermi 'leggimi la nota [titolo]' per vederne i dettagli._"
    await reply(update, msg, True)


async def handle_add_expense(update: Update, f: SimpleNamespace, user_text: str):
    text = await finance_nl.add_expense(update.effective_user.id, f.amount, f.finance_category, f.title, f.start_str)
    await reply(update, text, True)


async def handle_add_income(update: Update, f: SimpleNamespace, user_text: str):
    text = await finance_nl.add_income(update.effective_user.id, f.amount, f.finance_category, f.title, f.start_str)
    await reply(update, text, True)


async def handle_finance_summary(update: Update, f: SimpleNamespace, user_text: str):
    text = await finance_nl.get_summary(update.effective_user.id, f.period, f.finance_category, f.start_str, f.end_str)
    await reply(update, text, True)


async def handle_server_status(update: Update, f: SimpleNamespace, user_text: str):
    """«Come sta il server?»: stesso rapporto del comando /server."""
    await _send_server_status(update.get_bot(), update.effective_chat.id)


async def handle_get_news(update: Update, f: SimpleNamespace, user_text: str):
    """«Dimmi le ultime news sul calcio»: le più recenti del tema, senza toccare lo stato dei riepiloghi."""
    groups, chosen = await asyncio.to_thread(news_service.collect_latest, f.news_topic)
    sections = digest.build_latest(groups, chosen, f.news_topic)
    if not sections:
        quale = f" su «{f.news_topic}»" if chosen else ""
        await reply(update, f"📰 Non ho trovato notizie{quale}. Controlla i feed in Nextcloud News (colonna EC).")
        return
    await _send_chunks(update.get_bot(), update.effective_chat.id, digest.chunk_sections(sections))


FINANCE_INTENTS = {"add_expense", "add_income", "get_finance_summary"}

HANDLERS = {
    "get_news": handle_get_news,
    "get_server_status": handle_server_status,
    "add_expense": handle_add_expense,
    "add_income": handle_add_income,
    "get_finance_summary": handle_finance_summary,
    "delete_event": handle_delete,
    "update_event": handle_update,
    "find_free_slots": handle_free_slots,
    "create_event": handle_create,
    "get_events": handle_get_events,
    "add_note": handle_add_note,
    "get_notes": handle_get_notes,
}


# ---------------------------------------------------------------------------
# Messaggi di testo
# ---------------------------------------------------------------------------

def _parse_with_memory(user_text: str, current_dt: str) -> dict:
    """Come parse_user_input, ma con le cose che mi hai chiesto di ricordare (se ci sono)."""
    memory = memory_service.prompt_block()
    return parse_user_input(user_text, current_dt, memory=memory) if memory else parse_user_input(user_text, current_dt)


async def dispatch(update: Update, user_text: str):
    current_dt = datetime.now(TZ_ROMA).strftime("%Y-%m-%d %H:%M:%S")

    # Mentre Gemini interpreta il messaggio, scaldo in parallelo la connessione a Nextcloud
    t0 = time.perf_counter()
    tasks = [asyncio.to_thread(_parse_with_memory, user_text, current_dt)]
    if can_use_calendar(update):
        tasks.append(asyncio.to_thread(prewarm))
    parsed = (await asyncio.gather(*tasks))[0]
    logger.info("[timing] interpretazione: %.1fs (intent=%s)", time.perf_counter() - t0, parsed.get("intent"))

    if parsed.get("error"):
        await reply(update, "❌ Non riesco a interpretare il messaggio (Gemini non risponde). Riprova tra poco.")
        return
    if parsed.get("intent") and not plugins.intent_on(parsed.get("intent")):
        p = plugins.plugin_for_intent(parsed.get("intent"))
        await reply(update, f"🧩 Il modulo «{p.title}» non è attivo su questo {ASSISTANT_NAME}.")
        return

    f = extract_fields(parsed)
    handler = HANDLERS.get(f.intent)
    if handler is None:
        if can_use_calendar(update):
            await reply(update, f"🤖 Ho ricevuto il messaggio ma non ho capito l'azione richiesta:\n`{parsed}`", True)
        else:
            await reply(update, "🤖 Non ho capito. Posso aiutarti con le finanze, ad esempio: «ho speso 10 euro di pizza» o /aiuto.")
        return

    # Ogni funzione ha il suo elenco di utenti: finanze (ADMIN_IDS + tu), calendario/note (solo tu)
    if f.intent in FINANCE_INTENTS:
        if not can_use_finance(update):
            await deny(update, " alle finanze")
            return
    elif not can_use_calendar(update):
        await deny(update, " a usare il calendario")
        return

    t1 = time.perf_counter()
    await handler(update, f, user_text)
    logger.info("[timing] esecuzione %s: %.1fs", f.intent, time.perf_counter() - t1)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gestisce i messaggi di testo e interpreta l'intento tramite Gemini."""
    if not (can_use_calendar(update) or can_use_finance(update)):
        await deny(update)
        return
    user_text = update.message.text
    if not user_text:
        return

    t0 = time.perf_counter()
    status = await update.message.reply_text("⏳ Elaboro...")
    try:
        await safe(update, dispatch(update, user_text))
    finally:
        logger.info("[timing] richiesta completata in %.1fs", time.perf_counter() - t0)
        try:
            await status.delete()
        except Exception:
            pass


def _features_for(user_id: int) -> frozenset:
    """Cosa può fare ognuno: calendario/note/promemoria/notizie/server (CALENDAR_USER_IDS) e finanze (FINANCE_USER_IDS)."""
    return features_for_ids(user_id, CALENDAR_USER_IDS, FINANCE_USER_IDS)


# Il motore è uno solo (services/assistant.py), lo stesso della pagina di EDITH: qui c'è solo lo sportello di Telegram.
bridge = EngineBridge(_features_for, prewarm=prewarm)


async def route_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Testo libero: se sto raccogliendo dati per un obiettivo di risparmio va al FinanceBot, altrimenti a EDITH."""
    if context.user_data.get("obj_step"):
        await obiettivi_input(update, context)
        return
    if ENGINE_UNIFIED:
        await bridge.on_text(update, context)          # motore unico
    else:
        await handle_message(update, context)          # vecchio percorso (ENGINE_UNIFIED=0)


# --- Promemoria finanze (prima erano su un AsyncIOScheduler a parte) ---
async def job_fin_daily(context: ContextTypes.DEFAULT_TYPE):
    await fin_scheduler.daily_summary(context.application)


async def job_fin_weekly(context: ContextTypes.DEFAULT_TYPE):
    await fin_scheduler.weekly_summary(context.application)


async def job_fin_budget(context: ContextTypes.DEFAULT_TYPE):
    await fin_scheduler.budget_check(context.application)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Errore nel bot", exc_info=context.error)


async def post_init(application: Application):
    """All'avvio scalda la connessione a Nextcloud e accende la web app (se attiva)."""
    await asyncio.to_thread(prewarm)
    if WEB_ENABLED:
        try:
            from web.server import start_web
            application.bot_data["web_task"] = await start_web()
        except Exception:          # la web app non deve mai impedire al bot di partire
            logger.exception("Web app non avviata")



async def post_shutdown(application: Application):
    for key in ("web_task",):
        task = application.bot_data.get(key)
        if task is not None:
            from web.server import stop_web
            await stop_web(task)


def build_finance_conversations() -> list:
    """Le tre conversazioni guidate del FinanceBot (/spesa, /entrata, /budget)."""
    text = filters.TEXT & ~filters.COMMAND
    fallbacks = [CommandHandler("annulla", fin_cancel)]
    return [
        ConversationHandler(
            entry_points=[CommandHandler("spesa", spesa_start)],
            states={
                SPESA_CAT: [CallbackQueryHandler(spesa_categoria, pattern="^cat_")],
                SPESA_IMP: [MessageHandler(text, spesa_importo)],
                SPESA_NOTA: [
                    MessageHandler(text, spesa_nota),
                    CallbackQueryHandler(spesa_nota_skip, pattern="^nota_skip_spesa$"),
                ],
            },
            fallbacks=fallbacks, per_message=False,
        ),
        ConversationHandler(
            entry_points=[CommandHandler("entrata", entrata_start)],
            states={
                ENTRATA_TIPO: [CallbackQueryHandler(entrata_tipo, pattern="^inc_")],
                ENTRATA_IMP: [MessageHandler(text, entrata_importo)],
                ENTRATA_NOTA: [
                    MessageHandler(text, entrata_nota),
                    CallbackQueryHandler(entrata_nota_skip, pattern="^nota_skip_entrata$"),
                ],
            },
            fallbacks=fallbacks, per_message=False,
        ),
        ConversationHandler(
            entry_points=[CommandHandler("budget", budget_start)],
            states={
                BUDGET_CAT: [CallbackQueryHandler(budget_categoria, pattern="^bcat_")],
                BUDGET_IMP: [MessageHandler(text, budget_importo)],
            },
            fallbacks=fallbacks, per_message=False,
        ),
    ]


def main():
    """Avvia il bot Telegram."""
    if not CALENDAR_USER_IDS and not FINANCE_USER_IDS:
        logger.warning("Né TELEGRAM_CHAT_ID né ADMIN_IDS impostati: il bot risponde a CHIUNQUE!")
    if not TELEGRAM_CHAT_ID:
        logger.warning("TELEGRAM_CHAT_ID non impostato: i promemoria del calendario sono disattivati.")

    if FINANCE:
        fin_db.init_db()

    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .defaults(Defaults(tzinfo=TZ_ROMA))   # orari dei job (riepiloghi finanze) in ora italiana
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    jq = application.job_queue
    if jq:
        if plugins.on("calendar"):
            jq.run_repeating(check_upcoming_events, interval=300, first=10)
        if TELEGRAM_CHAT_ID:
            jq.run_daily(job_morning, time=MORNING_TIME, name="morning")
            if plugins.on("news"):
                jq.run_daily(job_news, time=NEWS_TIME, name="news_update")
            if MON_ENABLED and plugins.on("server"):
                jq.run_repeating(job_monitor, interval=MON_INTERVAL, first=20, name="monitor")
            if plugins.on("calendar") or plugins.on("news"):
                jq.run_daily(job_evening, time=NEWS_EVENING_TIME, name="news_evening")
            logger.info("Buongiorno alle %s, news alle %s, sera alle %s", MORNING_TIME.strftime("%H:%M"),
                        NEWS_TIME.strftime("%H:%M"), NEWS_EVENING_TIME.strftime("%H:%M"))
        if FINANCE:
            jq.run_daily(job_fin_daily, time=dtime(21, 0), name="fin_daily")
            jq.run_daily(job_fin_weekly, time=dtime(20, 0), days=(0,), name="fin_weekly")   # 0 = domenica
            jq.run_daily(job_fin_budget, time=dtime(12, 0), name="fin_budget")
        if FINANCE and CHARTS_ENABLED and TELEGRAM_CHAT_ID:
            jq.run_daily(job_charts, time=CHARTS_TIME, name="charts")
            logger.info("Grafici in Nextcloud ogni giorno alle %s", CHARTS_TIME.strftime("%H:%M"))
    else:
        logger.warning("JobQueue non disponibile: promemoria disattivati (serve python-telegram-bot[job-queue])")

    # Le conversazioni guidate vanno registrate per prime: hanno la precedenza sul testo libero
    if FINANCE:
        for conv in build_finance_conversations():
            application.add_handler(conv)

    # Base: sempre presenti
    application.add_handler(CommandHandler(["start", "aiuto", "help"], cmd_start))
    application.add_handler(CommandHandler("annulla", cmd_annulla))
    application.add_handler(CommandHandler(["sveglia", "briefing"], cmd_sveglia))
    application.add_handler(CallbackQueryHandler(bridge.on_callback, pattern=r"^eng:"))     # pulsanti Sì/No e scelte del motore unico

    # Moduli: ogni comando solo se il suo modulo è attivo
    if plugins.on("calendar"):
        application.add_handler(CommandHandler("oggi", cmd_oggi))
        application.add_handler(CommandHandler("domani", cmd_domani))
    if plugins.on("calendar") or plugins.on("news"):
        application.add_handler(CommandHandler("sera", cmd_sera))
    if plugins.on("weather"):
        application.add_handler(CommandHandler("meteo", cmd_meteo))
    if plugins.on("server"):
        application.add_handler(CommandHandler("server", cmd_server))
        application.add_handler(CommandHandler("boot", cmd_boot))
        application.add_handler(CommandHandler("silenzia", cmd_silenzia))
    if plugins.on("news"):
        application.add_handler(CommandHandler("news", cmd_news))
        application.add_handler(CommandHandler("newsstato", cmd_newsstato))
    if FINANCE:                                          # /oggi è del calendario: il riepilogo di oggi è /riepilogo
        application.add_handler(CommandHandler("grafici", cmd_grafici))
        application.add_handler(CommandHandler("riepilogo", riepilogo_giornaliero))
        application.add_handler(CommandHandler("settimana", riepilogo_settimanale))
        application.add_handler(CommandHandler("mese", riepilogo_mensile))
        application.add_handler(CommandHandler("anno", riepilogo_annuale))
        application.add_handler(CommandHandler("grafico", grafico_categorie))
        application.add_handler(CommandHandler("trend", grafico_trend))
        application.add_handler(CommandHandler("budgetview", budget_view))
        application.add_handler(CommandHandler("cerca", cerca_transazione))
        application.add_handler(CommandHandler("elimina", elimina_ultima))
        application.add_handler(CommandHandler("esporta", esporta_csv))
        application.add_handler(CommandHandler("obiettivi", obiettivi_start))
        application.add_handler(CallbackQueryHandler(obiettivi_menu, pattern="^obj_"))

    # Testo libero. block=False: un messaggio lento (Gemini, Nextcloud) non ferma gli altri utenti
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.UpdateType.MESSAGE & filters.ChatType.PRIVATE,
            route_text,
            block=False,
        )
    )
    application.add_error_handler(on_error)

    logger.info("%s avviato · moduli: %s", ASSISTANT_NAME, ", ".join(plugins.enabled()) or "solo la base")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
