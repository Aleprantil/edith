"""
Monitoraggio del server.

- Legge direttamente da /proc e /sys (visibili anche dal container Docker): CPU, RAM, carico, temperature,
  ventole, batteria/rete elettrica, rete. Nessuna libreria esterna, pochi KB letti a ogni controllo.
- Legge il file della sonda sull'host (monitor/host_probe.py) per ciò che richiede privilegi: log del
  kernel, SMART, montaggi, container Docker, riepilogo dell'avvio.
- Decide quando mandare un avviso (con conferma su più controlli, per evitare falsi allarmi) e quando
  dire che il problema è rientrato.
"""
import glob
import json
import logging
import os
import shutil
import threading
import time
from collections import deque

from config import (
    MON_BATTERY_PCT, MON_BOOT_REPORT_DELAY, MON_CPU_MINUTES, MON_CPU_PCT, MON_DISK_PCT, MON_INTERVAL,
    MON_NEWS_STALE_HOURS, MON_PROBE_FILE, MON_RAM_FREE_PCT, MON_ROOT_PATH, MON_STATE_FILE, MON_TEMP_CPU, MON_TEMP_DISK,
)
from services import monitor_format as fmt
from services import news_service, nextcloud_service

logger = logging.getLogger(__name__)

PROC = "/proc"
SYS = "/sys"
PROBE_STALE_S = 300           # la sonda dovrebbe aggiornare ogni minuto
NC_EVERY_S = 120              # controllo di Nextcloud ogni 2 minuti
NEWS_EVERY_S = 1800           # controllo dell'aggiornamento dei feed ogni 30 minuti
KERNEL_MIN_GAP_S = 900        # al massimo un avviso sugli errori del kernel ogni 15 minuti (poi aggrega)


# ---------------------------------------------------------------------------
# Letture di /proc e /sys
# ---------------------------------------------------------------------------

def _read(path, default=None):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return default


def _num(text):
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def read_cpu_times(proc=None):
    """(tempo inattivo, tempo totale) in tick, da /proc/stat."""
    line = (_read(f"{proc or PROC}/stat", "") or "cpu 0 0 0 0").splitlines()[0]
    parts = [int(x) for x in line.split()[1:9]] + [0] * 8
    return parts[3] + parts[4], sum(parts[:8])


def cpu_percent(prev, cur):
    if not prev or not cur:
        return None
    d_total, d_idle = cur[1] - prev[1], cur[0] - prev[0]
    if d_total <= 0:
        return None
    return max(0.0, min(100.0, 100.0 * (1 - d_idle / d_total)))


def cpu_cores(proc=None):
    n = sum(1 for line in (_read(f"{proc or PROC}/stat", "") or "").splitlines()
            if line.startswith("cpu") and line[3:4].isdigit())
    return n or os.cpu_count() or 1


def read_memory(proc=None):
    kb = {}
    for line in (_read(f"{proc or PROC}/meminfo", "") or "").splitlines():
        k, _, v = line.partition(":")
        kb[k] = _num(v.split()[0]) if v.split() else None
    total, avail = kb.get("MemTotal") or 0, kb.get("MemAvailable") or 0
    used = max(0, total - avail)
    swap_t, swap_f = kb.get("SwapTotal") or 0, kb.get("SwapFree") or 0
    return {
        "total": total * 1024, "available": avail * 1024, "used": used * 1024,
        "pct_used": (100.0 * used / total) if total else None,
        "pct_free": (100.0 * avail / total) if total else None,
        "swap_total": swap_t * 1024, "swap_used": max(0, swap_t - swap_f) * 1024,
    }


def read_loadavg(proc=None):
    p = (_read(f"{proc or PROC}/loadavg", "") or "").split()
    try:
        return [float(x) for x in p[:3]]
    except ValueError:
        return []


def read_uptime(proc=None):
    try:
        return float((_read(f"{proc or PROC}/uptime", "") or "0").split()[0])
    except (ValueError, IndexError):
        return 0.0


def read_boot_id(proc=None):
    return _read(f"{proc or PROC}/sys/kernel/random/boot_id", "") or ""


def read_temps(sysdir=None):
    sysdir = sysdir or SYS
    out = []
    for hw in sorted(glob.glob(f"{sysdir}/class/hwmon/hwmon*")):
        chip = _read(f"{hw}/name", os.path.basename(hw))
        for f in sorted(glob.glob(f"{hw}/temp*_input")):
            base = f[:-len("_input")]
            val = _num(_read(f))
            if val is None:
                continue
            c = val / 1000.0
            if -30 < c < 130:
                out.append({"chip": chip, "label": _read(f"{base}_label", os.path.basename(base)), "c": c})
    for z in sorted(glob.glob(f"{sysdir}/class/thermal/thermal_zone*")):
        val = _num(_read(f"{z}/temp"))
        if val is not None and -30 < val / 1000.0 < 130:
            out.append({"chip": "thermal:" + (_read(f"{z}/type", "zone") or "zone"), "label": os.path.basename(z), "c": val / 1000.0})
    return out


def cpu_temp(temps):
    """Temperatura della CPU: pacchetto 'coretemp' se c'è, altrimenti la zona termica più adatta."""
    def pick(pred, prefer=None):
        c = [t for t in temps if pred(t)]
        if prefer:
            p = [t for t in c if prefer in t["label"].lower()]
            c = p or c
        return max((t["c"] for t in c), default=None)
    for pred, prefer in (
        (lambda t: t["chip"] in ("coretemp", "k10temp", "zenpower"), "package"),
        (lambda t: t["chip"] == "thermal:x86_pkg_temp", None),
        (lambda t: t["chip"] in ("acpitz", "thermal:acpitz", "cpu_thermal", "thermal:cpu-thermal"), None),
    ):
        v = pick(pred, prefer)
        if v is not None:
            return v
    return max((t["c"] for t in temps if t["chip"] not in ("drivetemp", "nvme")), default=None)


def read_fans(sysdir=None):
    out = []
    for hw in sorted(glob.glob(f"{sysdir or SYS}/class/hwmon/hwmon*")):
        for f in sorted(glob.glob(f"{hw}/fan*_input")):
            rpm = _num(_read(f))
            if rpm is not None:
                out.append({"chip": _read(f"{hw}/name", ""), "rpm": rpm})
    return out


def read_power(sysdir=None):
    """{"ac": True/False/None, "battery": {"pct","status"} o None}. ac=None se la macchina non lo dice."""
    mains, bat = [], None
    for d in sorted(glob.glob(f"{sysdir or SYS}/class/power_supply/*")):
        typ = _read(f"{d}/type")
        if typ == "Mains":
            mains.append(_read(f"{d}/online") == "1")
        elif typ == "Battery":
            bat = {"pct": _num(_read(f"{d}/capacity")), "status": _read(f"{d}/status", "")}
    ac = any(mains) if mains else None
    if ac is None and bat:
        ac = True if bat["status"] in ("Charging", "Full") else False if bat["status"] == "Discharging" else None
    return {"ac": ac, "battery": bat}


def read_net(sysdir=None):
    out = []
    for d in sorted(glob.glob(f"{sysdir or SYS}/class/net/*")):
        name = os.path.basename(d)
        if name == "lo" or name.startswith(("veth", "docker", "br-")):
            continue
        try:
            flags = int(_read(f"{d}/flags", "0") or "0", 16)
        except ValueError:
            flags = 0
        out.append({"name": name, "up": bool(flags & 1) and _read(f"{d}/operstate") != "down",
                    "rx": _num(_read(f"{d}/statistics/rx_bytes")), "tx": _num(_read(f"{d}/statistics/tx_bytes"))})
    return out


def disk_usage(path):
    try:
        du = shutil.disk_usage(path)
    except OSError:
        return None
    return {"total": du.total, "used": du.used, "free": du.free, "pct": 100.0 * du.used / du.total if du.total else None}


def read_bot_memory(sysdir=None):
    """Memoria usata dal container di EDITH (cgroup v2, poi v1)."""
    for p in (f"{sysdir or SYS}/fs/cgroup/memory.current", f"{sysdir or SYS}/fs/cgroup/memory/memory.usage_in_bytes"):
        v = _num(_read(p))
        if v is not None:
            return v
    return None


def take_snapshot(prev_cpu=None, proc=None, sysdir=None, root_path=None):
    """Fotografia del server adesso. Ritorna (snapshot, campione_cpu_da_riusare)."""
    cur = read_cpu_times(proc)
    temps = read_temps(sysdir)
    snap = {
        "ts": time.time(), "cpu_pct": cpu_percent(prev_cpu, cur), "cores": cpu_cores(proc), "load": read_loadavg(proc),
        "mem": read_memory(proc), "uptime_s": read_uptime(proc), "boot_id": read_boot_id(proc),
        "temps": temps, "cpu_temp": cpu_temp(temps), "fans": read_fans(sysdir), "power": read_power(sysdir),
        "net": read_net(sysdir), "root": disk_usage(root_path or MON_ROOT_PATH), "bot_mem": read_bot_memory(sysdir),
    }
    return snap, cur


def read_probe(path=None, now=None):
    """(dati, età in secondi) del file della sonda, oppure (None, None) se manca o è illeggibile."""
    try:
        with open(path or MON_PROBE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data, (now or time.time()) - float(data["ts"])
    except (OSError, ValueError, KeyError, TypeError):
        return None, None


# ---------------------------------------------------------------------------
# Storico breve (in memoria)
# ---------------------------------------------------------------------------

class History:
    def __init__(self, maxlen=180):
        self.d = deque(maxlen=maxlen)

    def add(self, ts, cpu, temp, ram_pct):
        self.d.append((ts, cpu, temp, ram_pct))

    def avg_cpu(self, minutes, now):
        vals = [c for t, c, _, _ in self.d if c is not None and now - t <= minutes * 60 + MON_INTERVAL]
        return (sum(vals) / len(vals), len(vals)) if vals else (None, 0)

    def peak_temp(self, minutes, now):
        vals = [x for t, _, x, _ in self.d if x is not None and now - t <= minutes * 60 + MON_INTERVAL]
        return max(vals) if vals else None


# ---------------------------------------------------------------------------
# Avvisi
# ---------------------------------------------------------------------------

class Check:
    """Una condizione da tenere d'occhio. `bad` dice se adesso c'è un problema."""
    def __init__(self, key, bad, level, text, ok_text, confirm=2, clear=2, remind_s=6 * 3600):
        self.key, self.bad, self.level, self.text, self.ok_text = key, bool(bad), level, text, ok_text
        self.confirm, self.clear, self.remind_s = confirm, clear, remind_s


class AlertEngine:
    """
    Trasforma una serie di controlli in eventi: scatta solo dopo `confirm` controlli consecutivi cattivi,
    rientra dopo `clear` buoni, e ricorda un problema ancora aperto ogni `remind_s`.
    """
    def __init__(self, state):
        self.s = state.setdefault("alerts", {})

    def feed(self, checks, now):
        events, seen = [], set()
        for c in checks:
            seen.add(c.key)
            st = self.s.setdefault(c.key, {"active": False, "bad": 0, "good": 0, "since": None, "last_sent": 0, "level": c.level, "text": ""})
            if c.bad:
                st["bad"], st["good"] = st["bad"] + 1, 0
            else:
                st["good"], st["bad"] = st["good"] + 1, 0
            if not st["active"] and st["bad"] >= c.confirm:
                st.update(active=True, since=now, last_sent=now, level=c.level, text=c.text)
                events.append(("fire", c))
            elif st["active"] and c.bad:
                st["text"] = c.text
                if c.remind_s and now - st["last_sent"] >= c.remind_s:
                    st["last_sent"] = now
                    events.append(("remind", c))
            elif st["active"] and st["good"] >= c.clear:
                st.update(active=False, last_sent=now)
                events.append(("resolve", c))
        for key, st in self.s.items():          # controlli spariti (container rimosso, disco tolto): chiudo in silenzio
            if key not in seen and st["active"]:
                st.update(active=False, bad=0, good=0)
        return events

    def active(self):
        return [(k, st["level"], st["text"]) for k, st in sorted(self.s.items()) if st.get("active")]


def build_checks(snap, probe, probe_age, nc, history, probe_seen, now, news_age_h=None):
    """Elenco di tutti i controlli. `nc` = (ok, spiegazione, ms) oppure None se non ancora controllato."""
    e = fmt.esc
    checks = []

    t = snap["cpu_temp"]
    checks.append(Check("temp_cpu", t is not None and t >= MON_TEMP_CPU, "crit",
                        f"🔥 <b>CPU troppo calda</b>: {t:.0f} °C (soglia {MON_TEMP_CPU:.0f}). Controlla ventola e polvere." if t is not None else "",
                        f"✅ Temperatura CPU rientrata: {t:.0f} °C." if t is not None else "✅ Temperatura CPU rientrata.", confirm=2))

    avg, n = history.avg_cpu(MON_CPU_MINUTES, now)
    expected = max(1, int(MON_CPU_MINUTES * 60 / MON_INTERVAL) - 1)
    checks.append(Check("cpu_high", avg is not None and n >= expected and avg >= MON_CPU_PCT, "warn",
                        f"⚠️ <b>CPU al {avg:.0f}%</b> da almeno {MON_CPU_MINUTES} minuti." if avg is not None else "",
                        "✅ Carico CPU tornato normale.", confirm=1, clear=3))

    free = snap["mem"]["pct_free"]
    checks.append(Check("ram_low", free is not None and free < MON_RAM_FREE_PCT, "warn",
                        f"⚠️ <b>RAM quasi esaurita</b>: disponibile solo il {free:.0f}%." if free is not None else "",
                        "✅ RAM di nuovo disponibile.", confirm=3, clear=3))

    root = snap.get("root")
    checks.append(Check("disk_root", bool(root and root["pct"] is not None and root["pct"] >= MON_DISK_PCT), "crit",
                        f"💽 <b>Disco di sistema pieno al {root['pct']:.0f}%</b>." if root and root["pct"] is not None else "",
                        "✅ Spazio sul disco di sistema di nuovo sotto la soglia.", confirm=1))

    p = snap["power"]
    bat = p.get("battery")
    checks.append(Check("power_ac", p.get("ac") is False, "crit",
                        "🔌 <b>Manca la corrente</b>: il server sta andando a batteria"
                        + (f" ({bat['pct']}%)." if bat and bat.get("pct") is not None else "."),
                        "✅ Corrente elettrica tornata.", confirm=2, clear=1))
    low = bool(bat and bat.get("pct") is not None and bat["pct"] <= MON_BATTERY_PCT and p.get("ac") is not True)
    checks.append(Check("battery_low", low, "crit",
                        f"🪫 <b>Batteria quasi scarica</b>: {bat['pct']}% senza corrente. Il server potrebbe spegnersi." if bat else "",
                        "✅ Batteria di nuovo sopra la soglia.", confirm=1, remind_s=1800))

    hw_disk = max((x["c"] for x in snap["temps"] if x["chip"] == "drivetemp"), default=None)
    checks.append(Check("temp_disk_hw", hw_disk is not None and hw_disk >= MON_TEMP_DISK, "warn",
                        f"🌡️ <b>Disco caldo</b>: {hw_disk:.0f} °C (soglia {MON_TEMP_DISK:.0f})." if hw_disk is not None else "",
                        "✅ Temperatura dei dischi rientrata.", confirm=2))

    if nc is not None:
        ok, why, ms = nc
        checks.append(Check("nextcloud", not ok, "crit", f"☁️ <b>Nextcloud non risponde</b>: {e(why)}.",
                            f"✅ Nextcloud risponde di nuovo{f' ({ms} ms)' if ms else ''}.", confirm=2, clear=1, remind_s=3 * 3600))

    if MON_NEWS_STALE_HOURS > 0:
        stale = news_age_h is not None and news_age_h > MON_NEWS_STALE_HOURS
        checks.append(Check("news_stale", stale, "warn",
                            f"📰 <b>I feed di News non si aggiornano</b>: l'articolo più recente è di {news_age_h:.0f} ore fa. "
                            "Controlla in Nextcloud i «Processi in background» (deve essere Cron) con /newsstato." if news_age_h is not None else "",
                            "✅ I feed di News si aggiornano di nuovo.", confirm=1, clear=1, remind_s=12 * 3600))

    checks.append(Check("probe_stale", bool(probe_seen and (probe is None or (probe_age or 0) > PROBE_STALE_S)), "warn",
                        ("🛰️ Il file della sonda dell'host è sparito" if probe is None
                         else f"🛰️ La sonda dell'host non aggiorna da {int((probe_age or 0) / 60)} minuti")
                        + ": timer fermo? (Senza sonda non vedo montaggi, log del kernel e SMART.)" if probe_seen else "",
                        "✅ La sonda dell'host è tornata attiva.", confirm=1, clear=1))

    if probe:
        mounts = probe.get("mounts") or []
        for m in mounts:
            path = m.get("path", "?")
            used_pct = (100.0 * m["used"] / m["total"]) if m.get("total") and m.get("used") is not None else None
            if not m.get("mounted"):
                txt = f"💿 <b>{e(path)} NON è montato</b>. Nextcloud e tutto ciò che sta lì non funzionano."
            elif m.get("ro"):
                txt = f"💿 <b>{e(path)} è montato in SOLA LETTURA</b>: il kernel lo fa dopo errori del disco."
            else:
                txt = ""
            checks.append(Check(f"mount_{path}", (not m.get("mounted")) or bool(m.get("ro")), "crit", txt,
                                f"✅ {e(path)} è di nuovo montato e scrivibile.", confirm=2, clear=2, remind_s=3600))
            checks.append(Check(f"disk_{path}", bool(used_pct is not None and used_pct >= MON_DISK_PCT), "crit",
                                f"💽 <b>{e(path)} pieno al {used_pct:.0f}%</b>." if used_pct is not None else "",
                                f"✅ Spazio su {e(path)} di nuovo sotto la soglia.", confirm=1))
            # container partiti prima del montaggio: non vedono i dati veri
            if m.get("mounted") and m.get("since_ts"):
                for c in probe.get("docker") or []:
                    stale = bool(c.get("started_ts") and any(str(s).startswith(path) for s in c.get("mounts", []))
                                 and c["started_ts"] < m["since_ts"] - 5)
                    checks.append(Check(f"stale_{c['name']}", stale, "crit",
                                        f"🐳 Il container <b>{e(c['name'])}</b> è partito PRIMA del montaggio di {e(path)}: non vede i dati veri. "
                                        f"Riavvialo: <code>docker restart {e(c['name'])}</code>",
                                        f"✅ {e(c['name'])} ora vede {e(path)}.", confirm=1, clear=1, remind_s=3600))

        for c in probe.get("docker") or []:
            should_run = c.get("restart_policy") in ("always", "unless-stopped", "on-failure")
            bad = (should_run and c.get("state") != "running") or c.get("state") == "restarting" or c.get("health") == "unhealthy"
            checks.append(Check(f"docker_{c['name']}", bad, "crit",
                                f"🐳 Il container <b>{e(c['name'])}</b> non funziona ({e(c.get('state', '?'))}"
                                f"{', ' + e(c['health']) if c.get('health') else ''}).",
                                f"✅ Il container {e(c['name'])} è di nuovo attivo.", confirm=2, clear=1, remind_s=3 * 3600))

        for d in probe.get("smart") or []:
            dev = d.get("dev", "?")
            bad_sectors = sum(x or 0 for x in (d.get("realloc"), d.get("pending"), d.get("uncorrect")))
            checks.append(Check(f"smart_{dev}", d.get("status") == "FAILED" or bad_sectors > 0, "crit",
                                f"🩺 <b>Disco {e(dev)}</b>: "
                                + ("SMART segnala un guasto. Fai subito un backup." if d.get("status") == "FAILED"
                                   else f"{bad_sectors} settori danneggiati/riallocati: il disco si sta usurando."),
                                f"✅ {e(dev)}: SMART di nuovo senza problemi.", confirm=1, remind_s=24 * 3600))
            temp = d.get("temp")
            checks.append(Check(f"temp_{dev}", temp is not None and temp >= MON_TEMP_DISK, "warn",
                                f"🌡️ <b>{e(dev)} a {temp} °C</b> (soglia {MON_TEMP_DISK:.0f}) all'ultima lettura SMART." if temp is not None else "",
                                f"✅ Temperatura di {e(dev)} rientrata.", confirm=1, remind_s=12 * 3600))
    return checks


# ---------------------------------------------------------------------------
# Orchestratore
# ---------------------------------------------------------------------------

def _load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(path, state):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, path)
    except OSError as err:
        logger.warning("Monitor: stato non salvato (%s)", err)


class Monitor:
    def __init__(self, state_path=None, probe_path=None, clock=time.time, nc_check=None, news_check=None):
        self.state_path = state_path or MON_STATE_FILE
        self.probe_path = probe_path or MON_PROBE_FILE
        self.clock = clock
        self.nc_check = nc_check or nextcloud_service.health_check
        self.state = _load_state(self.state_path)
        self.engine = AlertEngine(self.state)
        self.history = History()
        self.prev_cpu = None
        self._nc, self._nc_ts = None, 0.0
        self._news_age, self._news_ts = None, 0.0
        self.news_check = news_check or news_service.feed_status
        self.lock = threading.Lock()

    # ----- utilità -------------------------------------------------------
    def _nextcloud(self, now):
        if now - self._nc_ts >= NC_EVERY_S:
            try:
                self._nc = self.nc_check()
            except Exception as err:      # un controllo che si rompe non deve fermare il monitoraggio
                logger.warning("Monitor: controllo Nextcloud fallito: %s", err)
                self._nc = (False, f"controllo non riuscito ({err.__class__.__name__})", None)
            self._nc_ts = now
        return self._nc

    def _news_freshness(self, now, nc):
        """Età (in ore) dell'articolo più recente di tutti i feed. None se non si sa (Nextcloud giù, nessun articolo)."""
        if nc is not None and not nc[0]:
            return None                       # Nextcloud non risponde: ci pensa già l'altro avviso
        if now - self._news_ts >= NEWS_EVERY_S:
            try:
                rows = self.news_check(now)
                newest = max((r["newest_pub"] for r in rows if r.get("newest_pub")), default=None)
                self._news_age = (now - newest) / 3600 if newest else None
            except Exception as err:          # News assente o in errore: nessun falso allarme
                logger.info("Monitor: stato dei feed non disponibile: %s", err)
                self._news_age = None
            self._news_ts = now
        return self._news_age

    def mute(self, hours: float, now=None) -> str:
        now = now or self.clock()
        with self.lock:
            if hours <= 0:
                self.state["muted_until"] = None
                _save_state(self.state_path, self.state)
                return "🔔 Avvisi riattivati."
            until = now + hours * 3600
            self.state["muted_until"] = until
            _save_state(self.state_path, self.state)
            return fmt.mute_text(hours, until)

    # ----- ciclo periodico ----------------------------------------------
    def tick(self) -> list:
        """Un controllo. Ritorna i messaggi HTML da mandare (può essere vuoto)."""
        with self.lock:
            now = self.clock()
            snap, self.prev_cpu = take_snapshot(self.prev_cpu)
            self.history.add(now, snap["cpu_pct"], snap["cpu_temp"], snap["mem"]["pct_used"])
            probe, age = read_probe(self.probe_path, now)
            if probe is not None:
                self.state["probe_seen"] = True
            nc = self._nextcloud(now)

            checks = build_checks(snap, probe, age, nc, self.history, self.state.get("probe_seen"), now,
                                  self._news_freshness(now, nc) if MON_NEWS_STALE_HOURS > 0 else None)
            events = self.engine.feed(checks, now)
            msgs, muted = [], bool(self.state.get("muted_until") and now < self.state["muted_until"])

            if not muted:
                msgs += [fmt.alert_text(kind, c) for kind, c in events if c.text or kind == "resolve"]
            if self.state.get("muted_until") and now >= self.state["muted_until"]:
                self.state["muted_until"] = None
                msgs.append(fmt.unmute_text(self.engine.active()))

            kmsg = self._kernel_news(probe, snap, now, silent=muted)
            if kmsg:
                msgs.append(kmsg)
            bmsg = self._boot_report(snap, probe)
            if bmsg:
                msgs.append(bmsg)

            _save_state(self.state_path, self.state)
            return msgs

    def _kernel_news(self, probe, snap, now, silent=False):
        """Errori del kernel NUOVI (USB, disco, filesystem, tensione) dall'ultimo controllo, aggregati e a ritmo limitato."""
        if not probe or "kernel" not in probe:
            return None
        counts, st = probe["kernel"].get("counts") or {}, self.state
        if st.get("kernel_boot") != snap["boot_id"] or st.get("kernel_counts") is None:
            st["kernel_boot"], st["kernel_counts"] = snap["boot_id"], counts      # base di partenza: non avviso del passato
            return None
        delta = {k: max(0, counts.get(k, 0) - st["kernel_counts"].get(k, 0)) for k in counts}
        if sum(delta.values()) == 0:
            st["kernel_counts"] = counts
            return None
        if silent:
            st["kernel_counts"] = counts
            return None
        if now - st.get("kernel_alert_ts", 0) < KERNEL_MIN_GAP_S:
            return None                      # lascio accumulare: sarà aggregato nel prossimo avviso
        st["kernel_counts"], st["kernel_alert_ts"] = counts, now
        n_new = max(1, min(5, sum(delta.values())))          # solo gli eventi nuovi, non gli ultimi 5 dell'avvio
        return fmt.kernel_alert_text(delta, (probe["kernel"].get("events") or [])[-n_new:])

    def _boot_report(self, snap, probe):
        """Dopo un riavvio: riepilogo di cosa è successo prima e di come è partito il server."""
        st, boot_id = self.state, snap["boot_id"]
        if st.get("boot_id") is None:
            st["boot_id"], st["boot_reported"] = boot_id, boot_id       # prima esecuzione: niente messaggio
            return None
        if st["boot_id"] != boot_id:
            st["boot_id"], st["boot_reported"] = boot_id, None          # nuovo avvio
            st["kernel_counts"] = None
        if st.get("boot_reported") == boot_id or snap["uptime_s"] < MON_BOOT_REPORT_DELAY:
            return None
        rep = (probe or {}).get("boot_report")
        if rep and rep.get("boot_id") == boot_id:
            st["boot_reported"] = boot_id
            return fmt.boot_report_text(rep, probe, snap)
        if snap["uptime_s"] >= MON_BOOT_REPORT_DELAY + 300:               # la sonda non c'è: messaggio essenziale
            st["boot_reported"] = boot_id
            return fmt.boot_basic_text(snap)
        return None

    # ----- su richiesta ---------------------------------------------------
    def server_report(self) -> str:
        """Il comando /server: fotografia completa, con un campione di CPU di un secondo."""
        a = read_cpu_times()
        time.sleep(1)
        snap, _ = take_snapshot(a)
        now = self.clock()
        probe, age = read_probe(self.probe_path, now)
        try:
            nc = self.nc_check()
        except Exception as err:
            nc = (False, f"controllo non riuscito ({err.__class__.__name__})", None)
        avg, _n = self.history.avg_cpu(MON_CPU_MINUTES, now)
        return fmt.server_report(snap, probe, age, nc, avg, self.history.peak_temp(60, now), self.engine.active(), now)

    def card(self) -> dict:
        """Dati strutturati per la scheda «Server» della web app (campione di CPU di 0,4 s, nessun invio)."""
        a = read_cpu_times()
        time.sleep(0.4)
        snap, _ = take_snapshot(a)
        now = self.clock()
        probe, age = read_probe(self.probe_path, now)
        nc = self._nc if self._nc is not None else self._nextcloud(now)
        avg, _n = self.history.avg_cpu(MON_CPU_MINUTES, now)
        mem = snap["mem"]
        probe = probe or {}
        mounts = []
        for mt in probe.get("mounts") or []:
            pct = (100.0 * mt["used"] / mt["total"]) if mt.get("total") and mt.get("used") is not None else None
            mounts.append({"path": mt.get("path"), "mounted": bool(mt.get("mounted")), "ro": bool(mt.get("ro")),
                           "used": mt.get("used"), "total": mt.get("total"), "pct": pct, "fstype": mt.get("fstype", "")})
        return {
            "host": probe.get("hostname") or os.uname().nodename,
            "uptime_s": snap["uptime_s"],
            "cpu": {"pct": snap.get("cpu_pct"), "avg": avg, "cores": snap["cores"], "load": snap.get("load") or []},
            "mem": {"used": mem["used"], "total": mem["total"], "pct": mem.get("pct_used"),
                    "swap_used": mem.get("swap_used"), "swap_total": mem.get("swap_total")},
            "temp": {"cpu": snap.get("cpu_temp"), "peak": self.history.peak_temp(60, now),
                     "fans": [f["rpm"] for f in snap.get("fans") or []],
                     "disks": [{"dev": d.get("dev"), "temp": d.get("temp")} for d in probe.get("smart") or [] if d.get("temp") is not None]},
            "root": snap.get("root"),
            "mounts": mounts,
            "power": snap.get("power") or {},
            "net": [{"name": i["name"], "up": i["up"]} for i in snap.get("net") or []],
            "nextcloud": ({"ok": nc[0], "why": nc[1], "ms": nc[2]} if nc else None),
            "docker": [{"name": c.get("name"), "state": c.get("state")} for c in probe.get("docker") or []],
            "kernel": (probe.get("kernel") or {}).get("counts") or {},
            "smart": [{"dev": d.get("dev"), "status": d.get("status"), "timeout": bool(d.get("timeout"))} for d in probe.get("smart") or []],
            "alerts": [{"level": lvl, "text": text} for _k, lvl, text in self.engine.active() if text],
            "probe": bool(probe), "probe_age": age,
            "bot_mem": snap.get("bot_mem"),
        }

    def boot_report_text(self) -> str:
        probe, _age = read_probe(self.probe_path, self.clock())
        rep = (probe or {}).get("boot_report")
        if not rep:
            return ("ℹ️ Non ho ancora un riepilogo dell'avvio: serve la sonda dell'host "
                    "(<code>sudo bash monitor/install_monitor.sh</code>).")
        snap, _ = take_snapshot(None)
        return fmt.boot_report_text(rep, probe, snap)


MONITOR = None


def get_monitor() -> Monitor:
    global MONITOR
    if MONITOR is None:
        MONITOR = Monitor()
    return MONITOR
