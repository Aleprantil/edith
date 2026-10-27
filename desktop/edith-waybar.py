#!/usr/bin/env python3
"""
EDITH nella barra di Hyprland (Waybar).

    edith-waybar.py           stampa il JSON per Waybar: prossimo impegno, promemoria scaduti, stato del server
    edith-waybar.py --open    apre EDITH nel browser
    edith-waybar.py --ask     chiede qualcosa a EDITH (finestra di input con fuzzel, wofi o rofi) e mostra la risposta in una notifica
    edith-waybar.py --quote   cambia la frase del giorno (e aggiorna la barra)

Legge i dati che EDITH già mostra nella pagina (le stesse API), passando da Tailscale: il server ti riconosce dal tuo
accesso Tailscale, quindi non serve nessuna password qui. Solo libreria standard di Python, nessuna dipendenza.

Impostazioni (variabili d'ambiente o ~/.config/edith/waybar.conf, una per riga, NOME=valore):
    EDITH_URL     indirizzo di EDITH            (di serie: https://edith.your-tailnet.ts.net:8443)
    EDITH_SIGNAL  numero del segnale di Waybar  (di serie: 8, deve coincidere con "signal" nel modulo)
    EDITH_BAR_STYLE  compact (di serie: pallino, minuti se manca meno di un'ora, scaduti) oppure full (anche il titolo)
"""
import concurrent.futures
import hashlib
import html
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

DEFAULT_URL = "https://edith.your-tailnet.ts.net:8443"
TIMEOUT = 4                                   # secondi per richiesta: la barra non deve mai restare appesa
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "edith-waybar"
CONF = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "edith" / "waybar.conf"
TITLE_MAX = 24


# ---------------------------------------------------------------------------
# Impostazioni
# ---------------------------------------------------------------------------

def settings() -> dict:
    conf = {"EDITH_URL": DEFAULT_URL, "EDITH_SIGNAL": "8", "EDITH_BAR_STYLE": "compact"}
    try:
        for line in CONF.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                conf[k.strip()] = v.strip().strip('"\'')
    except OSError:
        pass
    for k in conf:
        if os.environ.get(k):
            conf[k] = os.environ[k]
    conf["EDITH_URL"] = conf["EDITH_URL"].rstrip("/")
    return conf


# ---------------------------------------------------------------------------
# Rete
# ---------------------------------------------------------------------------

class AuthError(Exception):
    """EDITH risponde, ma non riconosce questo PC (accesso Tailscale non ammesso)."""


def http_json(url: str, body: dict = None, timeout: float = TIMEOUT):
    headers = {"Accept": "application/json", "User-Agent": "edith-waybar"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers.update({"Content-Type": "application/json", "X-Requested-With": "edith"})
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise AuthError(f"HTTP {e.code}") from e
        raise


def fetch_cards(base: str) -> dict:
    """Le tre schede che servono, in parallelo. Una scheda che fallisce non blocca le altre."""
    out = {}

    def one(name):
        r = http_json(f"{base}/api/cards/{name}")
        return name, (r.get("data") if r.get("ok") else None)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(one, n) for n in ("today", "tasks", "server")]
        errors = []
        for f in futures:
            try:
                name, data = f.result()
                out[name] = data
            except AuthError:
                raise
            except Exception as e:                        # rete giù, timeout, risposta strana
                errors.append(e)
    if not any(out.values()):
        raise ConnectionError(str(errors[0]) if errors else "nessun dato")
    return out


def load_cache(name: str):
    try:
        return json.loads((CACHE / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_cache(name: str, data) -> None:
    try:
        CACHE.mkdir(parents=True, exist_ok=True)
        tmp = CACHE / (name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(CACHE / name)
    except OSError:
        pass


def load_quotes(base: str) -> list:
    """Le frasi di EDITH, rilette dal server al massimo una volta al giorno."""
    cached = load_cache("frasi.json")
    if cached and time.time() - cached.get("at", 0) < 86400:
        return cached.get("frasi", [])
    try:
        frasi = [f for f in http_json(f"{base}/static/frasi.json").get("frasi", []) if isinstance(f, dict) and isinstance(f.get("t"), str)]
        save_cache("frasi.json", {"at": time.time(), "frasi": frasi})
        return frasi
    except Exception:
        return (cached or {}).get("frasi", [])


# ---------------------------------------------------------------------------
# Logica (funzioni pure: facili da provare)
# ---------------------------------------------------------------------------

esc = lambda s: html.escape(str(s or ""), quote=False)                 # il testo della barra usa il markup Pango


def short(s: str, n: int = TITLE_MAX) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def in_label(mins: int) -> str:
    if mins <= 0:
        return "ora"
    if mins < 60:
        return f"tra {mins} min"
    h, m = divmod(mins, 60)
    return f"tra {h} h" + (f" {m} min" if m else "")


def _dt(s):
    try:
        return datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


def next_event(today: dict, now: datetime):
    """Il prossimo impegno: quello in corso, poi il primo di oggi, poi il primo dei prossimi giorni. None se non c'è nulla."""
    if not today:
        return None
    timed = sorted(((e, _dt(e.get("start_iso")), _dt(e.get("end_iso"))) for e in today.get("events") or []), key=lambda x: x[1] or now)
    timed = [x for x in timed if x[1] and x[2]]
    for e, s, f in timed:
        if s <= now < f:
            return {"when": "in corso", "title": e.get("title"), "loc": e.get("location"), "extra": f"fino alle {f:%H:%M}", "now": True}
    for e, s, _f in timed:
        if s > now:
            mins = int((s - now).total_seconds() // 60) + 1
            return {"when": in_label(mins) if mins < 360 else f"alle {s:%H:%M}", "title": e.get("title"), "loc": e.get("location"), "mins": mins}
    for g in today.get("upcoming") or []:
        evs = sorted((x for x in ((e, _dt(e.get("start_iso"))) for e in g.get("events") or []) if x[1]), key=lambda x: x[1])
        if evs:
            e, s = evs[0]
            day = "domani" if g.get("label") == "Domani" else str(g.get("label", "")).split(" ")[0]
            return {"when": f"{day} {s:%H:%M}", "title": e.get("title"), "loc": e.get("location")}
    return None


def server_bad(server: dict) -> bool:
    if not server:
        return False
    nc = server.get("nextcloud") or {}
    return bool(server.get("alerts")) or nc.get("ok") is False or any(not m.get("mounted") or m.get("ro") for m in server.get("mounts") or [])


def flags(data: dict, now: datetime) -> dict:
    """Le stesse condizioni della pagina di EDITH, per scegliere frasi adatte al momento."""
    h, dow, mo = now.hour, now.weekday(), now.month                     # weekday: lunedì = 0
    f = {"mattina": 5 <= h < 12, "pomeriggio": 12 <= h < 18, "sera": 18 <= h < 23, "notte": h >= 23 or h < 5,
         "lunedi": dow == 0, "martedi": dow == 1, "mercoledi": dow == 2, "giovedi": dow == 3, "venerdi": dow == 4,
         "sabato": dow == 5, "domenica": dow == 6, "weekend": dow >= 5, "inverno": mo in (12, 1, 2, 3), "estate": mo in (6, 7, 8, 9)}
    t, tk, sv = data.get("today"), data.get("tasks"), data.get("server")
    if t:
        w = t.get("weather") or {}
        lab = str(w.get("label", "")).lower()
        evs = t.get("events") or []
        dom = next((g for g in t.get("upcoming") or [] if g.get("label") == "Domani"), {})
        text = lambda e: f"{e.get('title', '')} {e.get('calendar', '')}"
        f.update(pioggia=bool(re.search(r"piogg|rovesc|temporal|piovigg", lab)), neve=bool(re.search(r"neve|nevisc", lab)), sereno="sereno" in lab,
                 freddo=bool(w) and (w.get("tmax") or 99) <= 8, caldo=bool(w) and (w.get("tmax") or -99) >= 28,
                 esame=any(re.search(r"esam", e.get("title", ""), re.I) for e in evs + (dom.get("events") or [])),
                 allenamento=any(re.search(r"allenament|calcio|partita", text(e), re.I) for e in evs),
                 lezione=any(re.search(r"lezion|\blez\b|\blab\b|universit", text(e), re.I) for e in evs),
                 giornata_libera=not evs, giornata_piena=len(evs) >= 5)
    if tk:
        f.update(scaduti=(tk.get("overdue") or 0) > 0, tutto_fatto=(tk.get("total") or 0) == 0)
    if sv:
        bad = server_bad(sv)
        f.update(server_avviso=bad, server_ok=not bad)
    return f


def quote_of_the_day(frasi: list, data: dict, now: datetime, offset: int = 0):
    """Una frase al giorno, stabile per tutta la giornata (cambia con --quote). Preferisce, a giorni alterni, quelle del momento."""
    if not frasi:
        return None
    f = flags(data, now)
    ctx = [q for q in frasi if q.get("c") and all(f.get(k) for k in q["c"])]
    gen = [q for q in frasi if not q.get("c")]
    seed = int(hashlib.sha256(f"{now:%Y-%m-%d}:{offset}".encode()).hexdigest(), 16)
    pool = ctx if ctx and (seed % 5 < 3 or not gen) else (gen or frasi)
    return pool[seed % len(pool)]["t"]


def compose(data: dict, now: datetime, quote: str = None, stale_since: float = None, style: str = "compact") -> dict:
    """Il JSON per Waybar: testo breve, suggerimento con i dettagli, classe per i colori.
    compact: pallino, «in corso» o i minuti se manca meno di un'ora, scaduti. full: anche quando e titolo."""
    today, tasks, server = data.get("today") or {}, data.get("tasks") or {}, data.get("server") or {}
    nxt = next_event(today, now)
    bad, overdue = server_bad(server), (tasks.get("overdue") or 0)

    dot = '<span foreground="#ff5d4d">◉</span>' if bad else ('<span foreground="#8d8071">◌</span>' if stale_since else '<span foreground="#9be15d">◉</span>')
    parts = [dot]
    if style == "full":
        if nxt:
            parts.append(f'<span foreground="#ffc46b">{esc(nxt["when"])}</span> {esc(short(nxt["title"]))}')
        else:
            parts.append("libero")
    elif nxt and nxt.get("now"):
        parts.append('<span foreground="#ff9f1c">in corso</span>')
    elif nxt and nxt.get("mins") is not None and nxt["mins"] <= 60:
        parts.append(f'<span foreground="#ffc46b">{nxt["mins"]} min</span>')
    if overdue:
        parts.append(f'<span foreground="#ffb340">☑ {overdue}</span>')
    text = "  ".join(parts)

    cls = "offline" if stale_since else "alert" if bad else "overdue" if overdue else "now" if (nxt and nxt.get("now")) else "ok"

    tip = [f"<b>EDITH</b> · {esc(today.get('date_label') or now.strftime('%d/%m'))}"]
    if stale_since:
        tip.append(f'<span foreground="#8d8071">non raggiungibile: dati delle {datetime.fromtimestamp(stale_since):%H:%M}</span>')
    days = today.get("weather_days") or []
    if days:
        tip.append(" · ".join(f"{d.get('emoji', '')} {esc(d.get('label'))} {d.get('tmin')}–{d.get('tmax')}°" + (f" ({d['pop']}%)" if (d.get("pop") or 0) >= 50 else "") for d in days))
    sun = today.get("sun") or {}
    if sun.get("rise") or sun.get("set"):
        tip.append(f"🌅 {esc(sun.get('rise') or '–')}   🌇 {esc(sun.get('set') or '–')}")
    if nxt:
        tip.append("")
        tip.append(f"<b>Prossimo</b>  {esc(nxt['when'])} · {esc(nxt['title'])}" + (f" · {esc(nxt['extra'])}" if nxt.get("extra") else "") + (f" · 📍 {esc(nxt['loc'])}" if nxt.get("loc") else ""))
    evs = [e for e in today.get("events") or [] if not e.get("past")]
    if evs:
        tip.append("")
        tip.append("<b>Oggi</b>")
        for e in evs[:8]:
            when = "tutto il giorno" if e.get("all_day") else f"{e.get('time', '')}" + (f"–{e['end']}" if e.get("end") else "")
            tip.append(f"  {esc(when):<11} {esc(e.get('title'))}")
    items = [t for g in tasks.get("lists") or [] for t in g.get("items") or []]
    if items:
        tip.append("")
        tip.append(f"<b>Promemoria</b> ({tasks.get('total', len(items))} aperti" + (f", {overdue} scadut{'o' if overdue == 1 else 'i'}" if overdue else "") + ")")
        for t in items[:5]:
            col = {"overdue": "#ff5d4d", "today": "#ff9f1c"}.get(t.get("level"))
            lab = f' <span foreground="{col}">{esc(t.get("label"))}</span>' if col and t.get("label") else (f" {esc(t.get('label'))}" if t.get("label") else "")
            tip.append(f"  • {esc(t.get('title'))}{lab}")
    if server:
        cpu, mem, temp, root, nc = server.get("cpu") or {}, server.get("mem") or {}, server.get("temp") or {}, server.get("root") or {}, server.get("nextcloud") or {}
        bits = [f"CPU {cpu['pct']:.0f}%" if cpu.get("pct") is not None else None, f"RAM {mem['pct']:.0f}%" if mem.get("pct") is not None else None,
                f"{temp['cpu']:.0f}°" if temp.get("cpu") is not None else None, f"disco {root['pct']:.0f}%" if root.get("pct") is not None else None,
                (f"Nextcloud {nc['ms']} ms" if nc.get("ok") and nc.get("ms") else "Nextcloud ✓" if nc.get("ok") else "Nextcloud ✗") if nc else None]
        tip.append("")
        tip.append(f"<b>Server</b> {esc(server.get('host'))}  " + " · ".join(b for b in bits if b))
        for a in server.get("alerts") or []:
            tip.append(f'  <span foreground="#ff5d4d">⚠ {esc(re.sub(r"<[^>]+>", "", a.get("html") or ""))}</span>')
    if quote:
        tip.append("")
        tip.append(f"<i>› {esc(quote)}</i>")
    tip.append("")
    tip.append('<span foreground="#8d8071">clic: apri · destro: chiedi · centrale: altra frase</span>')
    return {"text": text, "tooltip": "\n".join(tip), "class": cls}


def failure(kind: str) -> dict:
    if kind == "auth":
        return {"text": '<span foreground="#ff5d4d">◉</span> EDITH: accesso negato', "class": "auth",
                "tooltip": "EDITH risponde ma non riconosce questo PC.\nIl PC deve essere collegato a Tailscale con il tuo account,\noppure il suo accesso va aggiunto a WEB_ALLOWED_LOGINS nel .env del server."}
    return {"text": '<span foreground="#8d8071">◌</span> EDITH offline', "class": "offline",
            "tooltip": "EDITH non risponde.\nControlla che Tailscale sia acceso sul PC e che il server sia raggiungibile."}


def session_id() -> str:
    host = re.sub(r"[^A-Za-z0-9_-]", "-", socket.gethostname())[:40] or "pc"
    return f"hypr-{host}".ljust(12, "x")[:64]


def plain(html_text: str) -> str:
    t = re.sub(r"<br\s*/?>", "\n", html_text or "")
    return html.unescape(re.sub(r"<[^>]+>", "", t)).strip()


# ---------------------------------------------------------------------------
# Azioni
# ---------------------------------------------------------------------------

def show_bar(conf: dict) -> None:
    now = datetime.now().astimezone()
    try:
        data = fetch_cards(conf["EDITH_URL"])
        save_cache("last.json", {"at": time.time(), "data": data})
        stale = None
    except AuthError:
        print(json.dumps(failure("auth"), ensure_ascii=False))
        return
    except Exception:
        last = load_cache("last.json")
        if not last or time.time() - last.get("at", 0) > 6 * 3600:         # dati troppo vecchi: meglio dire che è offline
            print(json.dumps(failure("offline"), ensure_ascii=False))
            return
        data, stale = last["data"], last["at"]
    offset = (load_cache("quote.json") or {}).get("offset", 0)
    quote = quote_of_the_day(load_quotes(conf["EDITH_URL"]), data, now, offset) if not stale else None
    print(json.dumps(compose(data, now, quote, stale, "full" if conf.get("EDITH_BAR_STYLE") == "full" else "compact"), ensure_ascii=False))


def ask_input(prompt: str = "EDITH › ") -> str:
    """Chiede il testo con il primo lanciatore disponibile."""
    for cmd in (["fuzzel", "--dmenu", "--prompt", prompt, "--lines", "0"], ["wofi", "--dmenu", "--prompt", prompt, "--lines", "1"],
                ["rofi", "-dmenu", "-p", prompt.strip()], ["tofi", f"--prompt-text={prompt}"]):
        if shutil.which(cmd[0]):
            try:
                r = subprocess.run(cmd, input="", capture_output=True, text=True, timeout=120)
                return r.stdout.strip()
            except (subprocess.SubprocessError, OSError):
                continue
    return ""


def notify(title: str, body: str) -> None:
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "-a", "EDITH", "-t", "12000", title, body[:900]], check=False)
    else:
        print(f"{title}\n{body}")


def ask(conf: dict, text: str = None) -> None:
    text = (text if text is not None else ask_input()).strip()
    if not text:
        return
    try:
        r = http_json(f"{conf['EDITH_URL']}/api/assistant", {"text": text[:500], "session": session_id()}, timeout=80)
    except AuthError:
        notify("EDITH", "Accesso negato: questo PC non è riconosciuto.")
        return
    except Exception:
        notify("EDITH", "Non riesco a raggiungere EDITH. Controlla Tailscale.")
        return
    body = plain(r.get("html") or r.get("error") or "")
    if r.get("confirm"):
        body += "\n\nServe una conferma: apri EDITH (clic sulla barra) e premi Sì o No."
    notify("EDITH" if r.get("ok", True) else "EDITH · errore", body or "…")
    refresh(conf)


def refresh(conf: dict) -> None:
    if shutil.which("pkill"):
        subprocess.run(["pkill", f"-RTMIN+{conf['EDITH_SIGNAL']}", "waybar"], check=False)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    conf = settings()
    if "--open" in argv:
        opener = shutil.which("xdg-open")
        if opener:
            subprocess.Popen([opener, conf["EDITH_URL"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return 0
    if "--ask" in argv:
        i = argv.index("--ask")
        ask(conf, " ".join(argv[i + 1:]) if len(argv) > i + 1 else None)
        return 0
    if "--quote" in argv:
        q = load_cache("quote.json") or {}
        save_cache("quote.json", {"offset": q.get("offset", 0) + 1})
        refresh(conf)
        return 0
    show_bar(conf)
    return 0


if __name__ == "__main__":
    sys.exit(main())
