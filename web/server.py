"""Avvio della web app dentro il processo del bot (stesso ciclo asyncio, stessi servizi)."""
import asyncio
import logging

import config
from services.assistant import Assistant
from web import cards, security
from web.app import create_app

logger = logging.getLogger(__name__)


LOOPBACK = ("127.0.0.1", "localhost", "::1")


def build_settings() -> security.WebSettings:
    local = config.WEB_HOST in LOOPBACK
    if not local:
        logger.warning("Web app: WEB_HOST=%s non è locale. Ignoro l'identità Tailscale (si può falsificare): resta valida solo la password.",
                       config.WEB_HOST)
    return security.WebSettings(
        allowed_logins=list(config.WEB_ALLOWED_LOGINS), password=config.WEB_PASSWORD,
        secret=security.make_secret(config.WEB_SECRET, config.TELEGRAM_BOT_TOKEN or ""),
        assistant_name=config.ASSISTANT_NAME, owner_name=config.OWNER_NAME, owner_nick=config.OWNER_NICKNAME, trust_header=local,
        quote_packs=list(config.QUOTE_PACKS))


def owner_id() -> int:
    for raw in (config.TELEGRAM_CHAT_ID,):
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            pass
    ids = sorted(config.FINANCE_USER_IDS or config.CALENDAR_USER_IDS or [])
    return ids[0] if ids else 0


async def start_web():
    """Avvia il server e ritorna il task (da cancellare allo spegnimento). Ritorna None se non può partire."""
    try:
        import uvicorn
    except ImportError:
        logger.warning("Web app non avviata: manca uvicorn (rifai la build: docker compose up -d --build)")
        return None

    settings = build_settings()
    cards.OWNER_ID = owner_id()
    app = create_app(settings, Assistant(owner_id=cards.OWNER_ID))
    cfg = uvicorn.Config(app, host=config.WEB_HOST, port=config.WEB_PORT, log_level="warning", access_log=False,
                         lifespan="off", timeout_keep_alive=15)
    server = uvicorn.Server(cfg)
    server.install_signal_handlers = lambda: None          # i segnali li gestisce il bot

    async def run():
        try:
            await server.serve()
        except (OSError, SystemExit) as e:
            # uvicorn chiama sys.exit() se non riesce ad ascoltare (es. porta occupata): senza questo spegnerebbe tutto il bot
            logger.error("Web app: impossibile ascoltare su %s:%s (%s)", config.WEB_HOST, config.WEB_PORT, e or "porta occupata?")
        except asyncio.CancelledError:
            server.should_exit = True
            raise
        except Exception:
            logger.exception("Web app: errore imprevisto, la fermo (il bot continua)")

    task = asyncio.create_task(run())
    task.edith_server = server
    if not settings.configured:
        logger.warning("Web app: nessun accesso configurato (WEB_ALLOWED_LOGINS o WEB_PASSWORD): mostrerà solo le istruzioni")
    logger.info("Web app in ascolto su http://%s:%s (esponila con: tailscale serve --bg --https=8443 %s)",
                config.WEB_HOST, config.WEB_PORT, config.WEB_PORT)
    return task


async def stop_web(task):
    if task is None:
        return
    server = getattr(task, "edith_server", None)
    if server is not None:
        server.should_exit = True
    try:
        await asyncio.wait_for(task, timeout=5)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        task.cancel()
