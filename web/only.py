"""
Solo la web app, SENZA il bot Telegram. Serve a provarla in una copia del progetto senza disturbare il bot vero
(due bot con lo stesso token Telegram si bloccano a vicenda e mandano messaggi doppi).

    python3 -m web.only            # porta WEB_PORT (di solito 8787)
    python3 -m web.only 8790       # porta a scelta, se quella è già usata dal bot vero

Usa i servizi VERI (Nextcloud, Gemini, finanze): eventi e note creati da qui finiscono davvero su Nextcloud.
Le finanze vanno nel database della cartella in cui lanci il comando (in una copia: la copia).
"""
import logging
import sys

import config
from services.assistant import Assistant
from web import cards
from web.app import create_app
from web.server import build_settings, owner_id


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    port = int(argv[0]) if argv and argv[0].isdigit() else config.WEB_PORT
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("web.only")
    try:
        import uvicorn
    except ImportError:
        log.error("Manca uvicorn: ricostruisci l'immagine (docker compose build).")
        return 1

    try:                                           # stesse tabelle che crea il bot all'avvio (non cancella nulla)
        import database
        database.init_db()
    except ImportError:
        log.info("Finanze non installate: la scheda Finanze resterà nascosta")

    settings = build_settings()
    cards.OWNER_ID = owner_id()
    app = create_app(settings, Assistant(owner_id=cards.OWNER_ID))
    log.warning("MODALITÀ SOLO WEB: il bot Telegram NON è avviato. Ascolto su http://%s:%s", config.WEB_HOST, port)
    log.info("Per aprirla da Tailscale: tailscale serve --bg --https=8443 %s", port)
    if not settings.configured:
        log.warning("Nessun accesso configurato (WEB_ALLOWED_LOGINS o WEB_PASSWORD nel .env): la pagina mostrerà solo le istruzioni")
    uvicorn.run(app, host=config.WEB_HOST, port=port, log_level="warning", access_log=False, lifespan="off")
    return 0


if __name__ == "__main__":
    sys.exit(main())
