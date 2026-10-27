"""
Web app di EDITH: pagina con la sfera e le schede, più le API che usa.

Accesso: identità Tailscale (header `Tailscale-User-Login` aggiunto da `tailscale serve`) oppure password.
Il servizio deve ascoltare SOLO su 127.0.0.1: in quel modo l'header non può essere falsificato da altri.
"""
import asyncio
import html
import json
import logging
import mimetypes
import os
import re
import struct

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from services import tasks_service
from services.tasks_service import TaskError
from web import cards, security
from web.security import COOKIE_NAME, COOKIE_TTL

logger = logging.getLogger(__name__)
mimetypes.add_type("application/manifest+json", ".webmanifest")
mimetypes.add_type("font/woff2", ".woff2")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
UID_RE = re.compile(r"^[A-Za-z0-9._@:+=,-]{1,200}$")        # identificativi dei promemoria (non si fidano mai dell'input)
MAX_BODY = 8 * 1024
MAX_AUDIO = 1_000_000          # byte: circa 30 secondi di voce a 16 kHz mono
MAX_SECONDS = 28
ASSISTANT_TIMEOUT = 75          # secondi: oltre, smetto di aspettare Gemini/Nextcloud e lo dico

SEC_HEADERS = {
    "Content-Security-Policy": ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                                "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "microphone=(self), camera=(), geolocation=()",
}

GATE = """<!doctype html>
<html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#070b14"><title>{name}</title>
<link rel="stylesheet" href="/static/style.css"><link rel="icon" href="/static/icon.svg"></head>
<body class="gate"><main class="gate-box">
<div class="gate-orb" aria-hidden="true"></div>
<h1>{name}</h1>
{body}
</main><script src="/static/gate.js"></script></body></html>"""


def wav_seconds(body: bytes):
    """Durata in secondi di un WAV PCM 16 bit mono a 16 kHz, oppure None se non è nel formato che ci aspettiamo."""
    if len(body) < 46 or body[:4] != b"RIFF" or body[8:12] != b"WAVE" or body[12:16] != b"fmt ":
        return None
    fmt, channels, rate, _byte_rate, _align, bits = struct.unpack("<HHIIHH", body[20:36])
    if fmt != 1 or channels != 1 or rate != 16000 or bits != 16 or body[36:40] != b"data":
        return None
    n = struct.unpack("<I", body[40:44])[0]
    if n <= 0 or n > len(body) - 44:
        return None
    return n / (rate * 2)


def create_app(settings: security.WebSettings, assistant, static_dir: str = STATIC_DIR) -> Starlette:
    limiter = security.LoginLimiter()
    rate = security.RateLimiter(30, 60)

    # ----- risposte ---------------------------------------------------------
    def jr(data, status=200, extra=None):
        return JSONResponse(data, status_code=status, headers={**SEC_HEADERS, "Cache-Control": "no-store", **(extra or {})})

    def gate_page(request, status=401):
        login = security.tailscale_login(request)
        name = html.escape(settings.assistant_name)
        parts = []
        if login and login not in settings.allowed_logins:
            parts.append(f"<p>Il tuo login Tailscale è <code>{html.escape(login)}</code>, ma non è tra quelli ammessi.</p>")
            if not settings.allowed_logins:
                parts.append("<p>Per consentirti l'accesso aggiungi questa riga al file <code>.env</code> e ricrea il container:</p>"
                             f"<pre>WEB_ALLOWED_LOGINS={html.escape(login)}</pre>")
            else:
                parts.append("<p>Se è il tuo, aggiungilo a <code>WEB_ALLOWED_LOGINS</code> nel file <code>.env</code> "
                             "(separati da virgola) e ricrea il container.</p>")
            status = 403
        elif not login and not settings.password:
            parts.append("<p>Questo indirizzo non passa da Tailscale, quindi non posso riconoscerti.</p>"
                         "<p>Aprilo con l'indirizzo <code>https://…ts.net:8443</code> oppure imposta <code>WEB_PASSWORD</code> nel <code>.env</code>.</p>")
        if settings.password:
            parts.append('<form id="loginform" autocomplete="on"><label for="pw">Password</label>'
                         '<input id="pw" name="password" type="password" autocomplete="current-password" required>'
                         '<button type="submit">Entra</button><p id="loginerr" class="err" hidden></p></form>')
        if not settings.configured and not login:
            parts.append("<p><b>Accesso non configurato.</b> Imposta <code>WEB_ALLOWED_LOGINS</code> o <code>WEB_PASSWORD</code> nel file <code>.env</code>.</p>")
        return HTMLResponse(GATE.format(name=name, body="\n".join(parts)), status_code=status,
                            headers={**SEC_HEADERS, "Cache-Control": "no-store"})

    def authed(request):
        return security.identify(request, settings)

    def post_ok(request) -> bool:
        """Difesa contro richieste inviate da altri siti: solo dalla nostra pagina (header personalizzato, JSON, stessa origine)."""
        return (not security.cross_site(request)
                and request.headers.get("x-requested-with") == "edith"
                and "application/json" in request.headers.get("content-type", ""))

    def audio_ok(request) -> bool:
        """Stesse difese dei messaggi di testo: solo dalla nostra pagina (header dedicato, stessa origine) e solo audio."""
        return (not security.cross_site(request)
                and request.headers.get("x-requested-with") == "edith"
                and request.headers.get("content-type", "").split(";")[0].strip().lower() in ("audio/wav", "audio/x-wav"))

    async def read_audio(request):
        """Il corpo della richiesta se è un WAV valido e di durata ragionevole, altrimenti None."""
        if int(request.headers.get("content-length") or 0) > MAX_AUDIO:
            return None
        body = await request.body()
        secs = wav_seconds(body) if len(body) <= MAX_AUDIO else None
        return body if secs is not None and 0.2 <= secs <= MAX_SECONDS else None

    async def read_json(request):
        if int(request.headers.get("content-length") or 0) > MAX_BODY:
            return None
        try:
            body = await request.body()
            data = json.loads(body) if body and len(body) <= MAX_BODY else None
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    # ----- pagine -----------------------------------------------------------
    async def index(request):
        if not authed(request):
            return gate_page(request)
        with open(os.path.join(static_dir, "index.html"), encoding="utf-8") as f:
            page = f.read().replace("{{NAME}}", html.escape(settings.assistant_name, quote=True))
        return HTMLResponse(page, headers={**SEC_HEADERS, "Cache-Control": "no-cache"})

    async def manifest(request):
        """Pubblico di proposito: il browser lo richiede senza cookie e contiene solo nome e icone."""
        name = settings.assistant_name
        data = {"name": name, "short_name": name[:12], "description": "Il tuo assistente personale", "start_url": "/", "scope": "/",
                "display": "standalone", "orientation": "portrait", "background_color": "#070605", "theme_color": "#070605",
                "icons": [{"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
                          {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
                          {"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml"}]}
        return Response(json.dumps(data), media_type="application/manifest+json", headers={**SEC_HEADERS, "Cache-Control": "no-cache"})

    async def static(request):
        name = request.path_params["name"]
        path = os.path.join(static_dir, name)
        if os.path.basename(name) != name or not os.path.isfile(path):
            return Response(status_code=404)
        if name == "frasi.json":
            return quotes_response(path)
        cache = "public, max-age=2592000, immutable" if name.endswith(".woff2") else "no-cache"      # i caratteri non cambiano: restano nel telefono
        return FileResponse(path, headers={**SEC_HEADERS, "Cache-Control": cache})

    def quotes_response(path):
        """Le frasi dei soli pacchetti scelti (EDITH_QUOTE_PACKS): quelle senza pacchetto sono per tutti."""
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return Response(status_code=404)
        packs = {p.lower() for p in settings.quote_packs}
        if packs:
            data["frasi"] = [f for f in data.get("frasi", []) if not f.get("p") or packs & set(f["p"])]
        return Response(json.dumps(data, ensure_ascii=False), media_type="application/json", headers={**SEC_HEADERS, "Cache-Control": "no-cache"})

    async def healthz(request):
        return jr({"ok": True})

    # ----- API --------------------------------------------------------------
    async def api_me(request):
        user = authed(request)
        if not user:
            return jr({"error": "non autorizzato"}, 401)
        return jr({"user": user, "assistant": settings.assistant_name, "owner": settings.owner_name,
                   "nick": settings.owner_nick or settings.owner_name, "cards": cards.active_order()})

    async def api_card(request):
        if not authed(request):
            return jr({"error": "non autorizzato"}, 401)
        name = request.path_params["name"]
        if name not in cards.CARDS:
            return jr({"error": "scheda sconosciuta"}, 404)
        try:
            data = await run_in_threadpool(cards.get_card, name, request.query_params.get("refresh") == "1")
            return jr({"ok": True, "data": data})
        except cards.CardUnavailable as e:
            return jr({"ok": False, "unavailable": True, "error": str(e)})
        except Exception as e:
            logger.exception("Scheda %s: errore", name)
            ours = type(e).__module__.startswith("services")        # CalendarError, NewsError, NotesError: messaggi già chiari
            return jr({"ok": False, "error": str(e)[:200] if ours else f"{type(e).__name__}: {str(e)[:160]}"})

    async def api_assistant(request):
        if not authed(request):
            return jr({"error": "non autorizzato"}, 401)
        if not post_ok(request):
            return jr({"error": "richiesta non valida"}, 400)
        data = await read_json(request)
        sid = str((data or {}).get("session") or "")
        text = (data or {}).get("text")
        if not data or not isinstance(text, str) or not SESSION_RE.match(sid):
            return jr({"error": "richiesta non valida"}, 400)
        if not rate.hit():
            return jr({"error": "Troppe richieste in poco tempo: aspetta un minuto."}, 429)
        try:
            reply = await asyncio.wait_for(run_in_threadpool(assistant.handle, sid, text, (data or {}).get("spoken") is True), ASSISTANT_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("Assistente: nessuna risposta entro %s s", ASSISTANT_TIMEOUT)
            return jr({"html": "❌ Ci sta mettendo troppo (Gemini o Nextcloud non rispondono). Riprova tra poco.", "ok": False,
                       "refresh": [], "confirm": None, "speak": ""})
        return jr(reply.to_dict())

    async def api_voice(request):
        """Ordine parlato: l'audio WAV va a Gemini per la trascrizione, poi segue la stessa strada del testo."""
        if not authed(request):
            return jr({"error": "non autorizzato"}, 401)
        sid = request.headers.get("x-session", "")
        if not audio_ok(request) or not SESSION_RE.match(sid):
            return jr({"error": "richiesta non valida"}, 400)
        wav = await read_audio(request)
        if wav is None:
            return jr({"error": "audio non valido o troppo lungo"}, 400)
        if not rate.hit():
            return jr({"error": "Troppe richieste in poco tempo: aspetta un minuto."}, 429)
        try:
            reply = await asyncio.wait_for(run_in_threadpool(assistant.handle_voice, sid, wav), ASSISTANT_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("Voce: nessuna risposta entro %s s", ASSISTANT_TIMEOUT)
            return jr({"html": "❌ Ci sta mettendo troppo (Gemini o Nextcloud non rispondono). Riprova tra poco.", "ok": False, "refresh": [],
                       "confirm": None, "speak": "Ci sto mettendo troppo, riprova tra poco.", "heard": ""})
        return jr(reply.to_dict())

    async def api_transcribe(request):
        """Solo la trascrizione (serve per rispondere «sì» o «no» a voce)."""
        if not authed(request):
            return jr({"error": "non autorizzato"}, 401)
        if not audio_ok(request):
            return jr({"error": "richiesta non valida"}, 400)
        wav = await read_audio(request)
        if wav is None:
            return jr({"error": "audio non valido o troppo lungo"}, 400)
        if not rate.hit():
            return jr({"error": "Troppe richieste in poco tempo: aspetta un minuto."}, 429)
        try:
            text = await asyncio.wait_for(run_in_threadpool(assistant.transcribe, wav), ASSISTANT_TIMEOUT)
        except asyncio.TimeoutError:
            return jr({"text": "", "error": "timeout"})
        except Exception:
            logger.exception("Trascrizione fallita")
            return jr({"text": "", "error": "trascrizione non riuscita"})
        return jr({"text": text})

    async def api_confirm(request):
        if not authed(request):
            return jr({"error": "non autorizzato"}, 401)
        if not post_ok(request):
            return jr({"error": "richiesta non valida"}, 400)
        data = await read_json(request)
        sid, token, yes = str((data or {}).get("session") or ""), (data or {}).get("token"), (data or {}).get("yes")
        if not data or not isinstance(token, str) or not isinstance(yes, bool) or not SESSION_RE.match(sid):
            return jr({"error": "richiesta non valida"}, 400)
        try:
            reply = await asyncio.wait_for(run_in_threadpool(assistant.confirm, sid, token, yes, (data or {}).get("spoken") is True), ASSISTANT_TIMEOUT)
        except asyncio.TimeoutError:
            return jr({"html": "❌ Nextcloud non risponde: controlla in agenda se l'operazione è andata a buon fine.", "ok": False,
                       "refresh": ["today"], "confirm": None, "speak": ""})
        return jr(reply.to_dict())

    def task_route(done: bool):
        async def handler(request):
            if not authed(request):
                return jr({"error": "non autorizzato"}, 401)
            if not post_ok(request):
                return jr({"error": "richiesta non valida"}, 400)
            data = await read_json(request)
            uid, lst = (data or {}).get("uid"), (data or {}).get("list")
            if not isinstance(uid, str) or not UID_RE.match(uid) or (lst is not None and (not isinstance(lst, str) or len(lst) > 80)):
                return jr({"error": "richiesta non valida"}, 400)
            try:
                res = await asyncio.wait_for(run_in_threadpool(tasks_service.set_done_by_uid, uid, done, lst or None), ASSISTANT_TIMEOUT)
            except TaskError as e:
                return jr({"ok": False, "error": str(e)})
            except asyncio.TimeoutError:
                return jr({"ok": False, "error": "Nextcloud non risponde: riprova tra poco."})
            except Exception:
                logger.exception("Promemoria: aggiornamento fallito")
                return jr({"ok": False, "error": "Non sono riuscito ad aggiornare il promemoria."})
            cards.invalidate("tasks")
            return jr({"ok": True, **res})
        return handler

    async def api_login(request):
        if not settings.password:
            return jr({"error": "accesso con password non attivo"}, 404)
        if not post_ok(request):
            return jr({"error": "richiesta non valida"}, 400)
        if not limiter.allowed():
            return jr({"error": "Troppi tentativi: riprova tra qualche minuto."}, 429)
        data = await read_json(request)
        if not data or not security.password_ok(settings, str(data.get("password") or "")):
            limiter.failed()
            return jr({"error": "Password errata."}, 401)
        limiter.reset()
        secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https" \
            or request.headers.get("host", "").split(":")[0].endswith(".ts.net")
        resp = jr({"ok": True})
        resp.set_cookie(COOKIE_NAME, security.make_cookie(settings), max_age=COOKIE_TTL, httponly=True,
                        samesite="strict", secure=secure, path="/")
        return resp

    async def api_logout(request):
        if not post_ok(request):
            return jr({"error": "richiesta non valida"}, 400)
        resp = jr({"ok": True})
        resp.delete_cookie(COOKIE_NAME, path="/")
        return resp

    routes = [
        Route("/", index),
        Route("/healthz", healthz),
        Route("/manifest.webmanifest", manifest),
        Route("/static/{name}", static),
        Route("/api/me", api_me),
        Route("/api/cards/{name}", api_card),
        Route("/api/assistant", api_assistant, methods=["POST"]),
        Route("/api/confirm", api_confirm, methods=["POST"]),
        Route("/api/tasks/complete", task_route(True), methods=["POST"]),
        Route("/api/tasks/reopen", task_route(False), methods=["POST"]),
        Route("/api/voice", api_voice, methods=["POST"]),
        Route("/api/transcribe", api_transcribe, methods=["POST"]),
        Route("/api/login", api_login, methods=["POST"]),
        Route("/api/logout", api_logout, methods=["POST"]),
    ]
    return Starlette(routes=routes)
