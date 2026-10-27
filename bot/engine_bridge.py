"""
Ponte tra il bot Telegram e il motore di EDITH (services/assistant.py).

Il motore è uno solo: capisce il messaggio, decide, chiede conferma, risponde. Qui c'è solo lo «sportello» di Telegram:
  - prende il testo libero e lo passa al motore;
  - mostra la risposta (HTML, spezzata se lunga) con i pulsanti Sì/No o la scelta tra più possibilità sotto il messaggio;
  - quando premi un pulsante, lo gira al motore (callback «eng:<codice>:<1|0>»).
Restano di Telegram, e non passano da qui: i comandi con la barra, i messaggi automatici e i flussi guidati delle finanze.
"""
import asyncio
import html
import logging
import re
import textwrap

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, TelegramError

from services.assistant import Assistant, Reply

logger = logging.getLogger(__name__)

MAX_LEN = 3900                      # Telegram accetta 4096 caratteri: lascio margine
TIMEOUT = 75                        # secondi
CALLBACK_RE = re.compile(r"^eng:([A-Za-z0-9_-]{6,40}):([01])$")


def features_for_ids(user_id, calendar_ids: set, finance_ids: set) -> frozenset:
    """Cosa può fare quell'utente. Lista vuota = nessun limite configurato (come nel resto del bot)."""
    feats = set()
    if not calendar_ids or user_id in calendar_ids:
        feats.add("calendar")
    if not finance_ids or user_id in finance_ids:
        feats.add("finance")
    return frozenset(feats)


# ---------------------------------------------------------------------------
# Come si mostra una risposta
# ---------------------------------------------------------------------------

def to_telegram_html(text: str) -> str:
    """L'HTML del motore, adattato a Telegram: niente <br> né attributi dei link che Telegram non conosce."""
    t = re.sub(r"<br\s*/?>", "\n", text or "")
    t = re.sub(r'\s+(?:target|rel)="[^"]*"', "", t)
    return t.strip() or "…"


def _plain(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]*>", "", text or ""))


def chunk_html(text: str, limit: int = MAX_LEN) -> list:
    """Spezza ai ritorni a capo (così i tag restano interi). Una riga più lunga del limite diventa testo semplice."""
    out, cur = [], ""
    for line in text.split("\n"):
        pieces = [line]
        if len(line) > limit:                                       # riga enorme: la tratto come testo, per non spezzare un tag a metà
            pieces = [html.escape(p, quote=False) for p in textwrap.wrap(_plain(line), limit, break_long_words=True)] or [""]
        for piece in pieces:
            if len(cur) + len(piece) + 1 > limit and cur:
                out.append(cur)
                cur = ""
            cur = f"{cur}\n{piece}" if cur else piece
    if cur.strip():
        out.append(cur)
    return out or ["…"]


def keyboard(confirm: dict):
    """I pulsanti sotto il messaggio: Sì/No, oppure un pulsante per ogni possibilità più Annulla."""
    if not confirm:
        return None

    def btn(label, token, yes):
        data = f"eng:{token}:{1 if yes else 0}"
        assert len(data.encode()) <= 64, data                      # limite di Telegram sui dati del pulsante
        return InlineKeyboardButton(label, callback_data=data)

    if confirm.get("choices"):
        rows = [[btn(f"{i + 1}. {c['label'][:40]}", c["token"], True)] for i, c in enumerate(confirm["choices"])]
        rows.append([btn("✖️ " + (confirm.get("no") or "Annulla"), confirm["token"], False)])
    else:
        rows = [[btn("✅ " + (confirm.get("yes") or "Sì"), confirm["token"], True), btn("✖️ " + (confirm.get("no") or "Annulla"), confirm["token"], False)]]
    return InlineKeyboardMarkup(rows)


async def send_reply(bot, chat_id, reply: Reply):
    """Manda la risposta. I pulsanti stanno sull'ultimo messaggio. Se Telegram non digerisce l'HTML, ripiega sul testo semplice."""
    chunks = chunk_html(to_telegram_html(reply.html))
    markup = keyboard(reply.confirm)
    last = None
    for i, chunk in enumerate(chunks):
        kwargs = {"reply_markup": markup} if (markup and i == len(chunks) - 1) else {}
        try:
            last = await bot.send_message(chat_id=chat_id, text=chunk, parse_mode="HTML", disable_web_page_preview=True, **kwargs)
        except BadRequest as e:
            logger.warning("Telegram non accetta l'HTML (%s): riinvio come testo semplice", e)
            last = await bot.send_message(chat_id=chat_id, text=_plain(chunk)[:4000] or "…", disable_web_page_preview=True, **kwargs)
    return last


def _failure(text: str) -> Reply:
    return Reply(html=text, ok=False)


# ---------------------------------------------------------------------------
# Lo sportello
# ---------------------------------------------------------------------------

class EngineBridge:
    def __init__(self, features_for, prewarm=None, make_engine=None, timeout: float = TIMEOUT):
        """features_for(user_id) -> insieme di permessi (vuoto = nessun accesso). prewarm: scalda la connessione a Nextcloud mentre Gemini pensa."""
        self.features_for = features_for
        self.prewarm = prewarm
        self.timeout = timeout
        self._make = make_engine or (lambda uid, feats: Assistant(owner_id=uid, features=feats))
        self._engines, self._locks = {}, {}

    def engine(self, user_id: int, features) -> Assistant:
        key = (user_id, frozenset(features))
        if key not in self._engines:
            self._engines[key] = self._make(user_id, frozenset(features))
        return self._engines[key]

    def _lock(self, user_id: int) -> asyncio.Lock:
        return self._locks.setdefault(user_id, asyncio.Lock())        # un messaggio alla volta per persona: l'ordine resta giusto

    async def _run(self, fn, *args) -> Reply:
        try:
            return await asyncio.wait_for(asyncio.to_thread(fn, *args), self.timeout)
        except asyncio.TimeoutError:
            logger.warning("Motore: nessuna risposta entro %s s", self.timeout)
            return _failure("❌ Ci sto mettendo troppo (Gemini o Nextcloud non rispondono). Riprova tra poco.")
        except Exception:
            logger.exception("Motore: errore non gestito")
            return _failure("❌ Qualcosa è andato storto. Riprova tra poco (i dettagli sono nei log).")

    async def on_text(self, update, context):
        user, message = update.effective_user, update.message
        text = (message.text or "").strip() if message else ""
        if not user or not text:
            return
        feats = self.features_for(user.id)
        if not feats:
            logger.warning("Accesso negato all'utente %s", user.id)
            await message.reply_text("⛔ Non sei autorizzato.")
            return
        engine, sid = self.engine(user.id, feats), f"tg-{update.effective_chat.id}"
        async with self._lock(user.id):
            status = await message.reply_text("⏳ Elaboro...")
            try:
                if self.prewarm and "calendar" in feats:                 # mentre Gemini interpreta, scaldo Nextcloud
                    asyncio.get_running_loop().run_in_executor(None, self._safe_prewarm)
                reply = await self._run(engine.handle, sid, text)
            finally:
                try:
                    await status.delete()
                except TelegramError:
                    pass
            await send_reply(context.bot, update.effective_chat.id, reply)

    def _safe_prewarm(self):
        try:
            self.prewarm()
        except Exception:
            logger.debug("prewarm fallito", exc_info=True)

    async def on_callback(self, update, context):
        q = update.callback_query
        m = CALLBACK_RE.match((q.data or "") if q else "")
        if not m:
            if q:
                await q.answer()
            return
        feats = self.features_for(q.from_user.id)
        if not feats:
            await q.answer("⛔ Non sei autorizzato.", show_alert=True)
            return
        await q.answer()                                                  # toglie l'orologio dal pulsante
        chat_id = q.message.chat_id if q.message else q.from_user.id
        engine, sid = self.engine(q.from_user.id, feats), f"tg-{chat_id}"
        token, yes = m.group(1), m.group(2) == "1"
        async with self._lock(q.from_user.id):
            try:
                await q.edit_message_reply_markup(reply_markup=None)      # una sola scelta: i pulsanti spariscono
            except TelegramError:
                pass
            reply = await self._run(engine.confirm, sid, token, yes)
            await send_reply(context.bot, chat_id, reply)
