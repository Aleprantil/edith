#!/usr/bin/env python3
"""
Sonda EDITH per l'HOST (non gira in Docker).

Raccoglie ciò che il container del bot non può vedere (log del kernel, SMART, montaggi, Docker, riepilogo
dell'avvio) e lo scrive in data/host_status.json. Lanciata ogni minuto da un timer systemd (vedi
install_monitor.sh). Solo libreria standard, SOLA LETTURA: non modifica nulla del sistema.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
OUT = Path(os.environ.get("PROBE_OUT") or BASE / "data" / "host_status.json")
STATE = Path(os.environ.get("PROBE_STATE") or "/var/lib/edith-probe/state.json")
MOUNTS = [m.strip() for m in (os.environ.get("PROBE_MOUNTS") or "/mnt/data").split(",") if m.strip()]
PROC = os.environ.get("PROBE_PROC") or "/proc"
SMART_EVERY = 6 * 3600           # SMART ogni 6 ore
# I dischi USB NON vengono interrogati: su alcuni adattatori il comando SMART va in timeout e il kernel fa un RESET della
# connessione USB (visto con smartctl a mano e ogni 6 ore dalla sonda). Si può riattivare con PROBE_SMART_USB=1.
SMART_USB = os.environ.get("PROBE_SMART_USB", "0").strip() == "1"
BOOT_GRACE = 15 * 60             # nei primi 15 minuti dall'avvio ricalcolo il riepilogo (il montaggio può arrivare tardi)

KERNEL_PATTERNS = {
    "usb": r"USB disconnect|reset (high|full|low|SuperSpeed)[- a-zA-Z]*USB device|uas_eh|device descriptor read|"
           r"unable to enumerate|over-current|xhci_hcd.*(died|halt|error)|disabled by hub|device not accepting address",
    "io": r"I/O error|Buffer I/O error|blk_update_request|critical medium error|Unrecovered read error|"
          r"FAILED Result|DID_(ERROR|BAD_TARGET|NO_CONNECT)|nvme[0-9]+.*(timeout|reset|I/O)",
    "fs": r"EXT4-fs (error|warning)|Remounting filesystem read-only|JBD2.*(abort|error)|XFS.*(error|shutdown)|"
          r"btrfs.*(error|corrupt)|exfat.*error|ntfs.*error",
    "power": r"[Uu]nder-?voltage|power cut|undervoltage",
}
CLEAN_SHUTDOWN = re.compile(r"Journal stopped|Reached target (Shutdown|Final Step|System (Power Off|Reboot))|"
                            r"reboot: (Restarting system|Power down|System halted)|systemd-shutdown|Shutting down", re.I)


# ---------------------------------------------------------------------------
# Utilità
# ---------------------------------------------------------------------------

def run2(cmd, timeout=20):
    """(stdout, stderr). Non solleva mai eccezioni: se il comando manca o scade, ritorna stringhe vuote."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace")
        return r.stdout or "", r.stderr or ""
    except (OSError, subprocess.SubprocessError):
        return "", ""


def run(cmd, timeout=20):
    return run2(cmd, timeout)[0]


def first_lines(cmd, n=3, timeout=20):
    """Prime n righe dell'output (senza leggere tutto: utile per log enormi)."""
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, errors="replace")
    except OSError:
        return []
    lines = []
    try:
        for _ in range(n):
            line = p.stdout.readline()
            if not line:
                break
            lines.append(line.rstrip("\n"))
    finally:
        p.kill()
        p.wait()
    return lines


def read(path, default=""):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return default


def iso_to_epoch(s):
    """'2026-10-03T03:48:20.123456789Z' -> epoch (secondi). None se non valida."""
    from datetime import datetime, timezone
    m = re.match(r"(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})", s or "")
    if not m or (s or "").startswith("0001-"):
        return None
    try:
        return datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def boot_ts():
    for line in read(f"{PROC}/stat").splitlines():
        if line.startswith("btime"):
            return int(line.split()[1])
    return int(time.time() - float(read(f"{PROC}/uptime", "0 0").split()[0]))


# ---------------------------------------------------------------------------
# Log del kernel
# ---------------------------------------------------------------------------

JOURNAL_LINE = re.compile(r"^(\d+(?:\.\d+)?)\s+\S+\s+kernel:\s*(.*)$")


def classify(msg):
    for kind, pat in KERNEL_PATTERNS.items():
        if re.search(pat, msg, re.I):
            return kind
    return None


def parse_kernel_events(text):
    events = []
    for line in text.splitlines():
        m = JOURNAL_LINE.match(line)
        if not m:
            continue
        kind = classify(m.group(2))
        if kind:
            events.append({"ts": float(m.group(1)), "kind": kind, "text": m.group(2)[:200]})
    return events


def kernel_events(boot="0"):
    """Tutti gli eventi significativi del log del kernel di un avvio (0 = attuale, -1 = precedente)."""
    pattern = "(?i)" + "|".join(KERNEL_PATTERNS.values())
    out, err = run2(["journalctl", "-k", "-b", boot, "-o", "short-unix", "--no-pager", "-g", pattern], timeout=60)
    if not out and re.search(r"unrecognized option|invalid option|unknown option", err, re.I):
        out = run(["journalctl", "-k", "-b", boot, "-o", "short-unix", "--no-pager"], timeout=120)   # journalctl senza -g
    return parse_kernel_events(out)


def kernel_summary(events):
    return {"counts": {k: sum(1 for e in events if e["kind"] == k) for k in KERNEL_PATTERNS}, "events": events[-40:]}


# ---------------------------------------------------------------------------
# Montaggi, Docker, SMART
# ---------------------------------------------------------------------------

def parse_findmnt(out):
    parts = out.strip().split(None, 2)
    if len(parts) < 2:
        return None
    return {"source": parts[0], "fstype": parts[1], "options": parts[2] if len(parts) > 2 else ""}


def mount_since(path):
    """Quando è diventato attivo il montaggio (epoch), secondo systemd; None se non si sa."""
    unit = run(["systemd-escape", "-p", "--suffix=mount", path]).strip()
    if not unit:
        return None
    val = run(["systemctl", "show", unit, "-p", "ActiveEnterTimestampMonotonic", "--value"]).strip()
    try:
        micro = int(val)
    except ValueError:
        return None
    return boot_ts() + micro / 1e6 if micro > 0 else None


def collect_mounts():
    out = []
    for mp in MOUNTS:
        info = {"path": mp, "mounted": False}
        found = parse_findmnt(run(["findmnt", "-rn", "-o", "SOURCE,FSTYPE,OPTIONS", mp]))
        if found and os.path.ismount(mp):
            info.update(found, mounted=True, ro="ro" in found["options"].split(","), since_ts=mount_since(mp))
            try:
                du = shutil.disk_usage(mp)
                info.update(total=du.total, used=du.used, free=du.free)
            except OSError:
                pass
        out.append(info)
    return out


DOCKER_FORMAT = ("{{.Name}}|{{.State.Status}}|{{.State.StartedAt}}|{{.RestartCount}}|{{.HostConfig.RestartPolicy.Name}}|"
                 "{{if .State.Health}}{{.State.Health.Status}}{{end}}|{{range .Mounts}}{{.Source}};{{end}}")


def parse_docker_inspect(out):
    rows = []
    for line in out.strip().splitlines():
        p = line.split("|")
        if len(p) < 7:
            continue
        rows.append({
            "name": p[0].lstrip("/"), "state": p[1], "started_ts": iso_to_epoch(p[2]),
            "restarts": int(p[3] or 0) if p[3].isdigit() else 0, "restart_policy": p[4], "health": p[5],
            "mounts": [m for m in p[6].split(";") if m],
        })
    return rows


def collect_docker():
    names = run(["docker", "ps", "-a", "--format", "{{.Names}}"]).split()
    return parse_docker_inspect(run(["docker", "inspect", "-f", DOCKER_FORMAT] + names)) if names else []


def _int(v):
    try:
        return int(re.match(r"-?\d+", str(v)).group(0))
    except (AttributeError, ValueError):
        return None


def parse_smart(text):
    d = {"status": "UNKNOWN", "timeout": bool(re.search(
        r"SMART Status command failed|Connection timed out|Read Device Identity failed", text, re.I))}
    if re.search(r"self-assessment test result: FAILED|FAILING_NOW", text, re.I):
        d["status"] = "FAILED"
    elif re.search(r"self-assessment test result: PASSED|SMART Health Status: OK", text, re.I):
        d["status"] = "PASSED"
    m = re.search(r"(?:Device Model|Model Number):\s*(.+)", text)
    d["model"] = m.group(1).strip() if m else ""
    d["ssd"] = bool(re.search(r"Solid State Device|NVMe", text))
    attrs = {}
    for line in text.splitlines():
        p = line.split()
        if len(p) >= 10 and p[0].isdigit():
            attrs[p[1]] = (_int(p[3]), _int(p[9]))
    raw = lambda n: attrs.get(n, (None, None))[1]          # noqa: E731
    d.update(hours=raw("Power_On_Hours"), cycles=raw("Power_Cycle_Count"), realloc=raw("Reallocated_Sector_Ct"),
             pending=raw("Current_Pending_Sector"), uncorrect=raw("Offline_Uncorrectable"), crc=raw("UDMA_CRC_Error_Count"),
             retract=raw("Power-Off_Retract_Count"), load_cycle_norm=attrs.get("Load_Cycle_Count", (None, None))[0])
    d["temp"] = raw("Temperature_Celsius")
    if d["temp"] is None:
        t = re.search(r"^Temperature:\s+(\d+)\s+Celsius", text, re.M)
        d["temp"] = int(t.group(1)) if t else None
    if re.search(r"NVMe", text):
        for key, pat in (("hours", r"Power On Hours:\s+([\d,]+)"), ("realloc", r"Media and Data Integrity Errors:\s+([\d,]+)")):
            m = re.search(pat, text)
            if m:
                d[key] = int(m.group(1).replace(",", ""))
    return d


def collect_smart(state, now):
    if state.get("smart") and now - state.get("smart_ts", 0) < SMART_EVERY:
        return state["smart"], state["smart_ts"]
    disks = []
    try:
        listing = json.loads(run(["lsblk", "-J", "-d", "-o", "NAME,TYPE,TRAN,MODEL"]) or "{}")
    except ValueError:
        listing = {}
    for dev in listing.get("blockdevices", []):
        if dev.get("type") != "disk":
            continue
        path = f"/dev/{dev['name']}"
        if (dev.get("tran") or "") == "usb" and not SMART_USB:
            disks.append({"dev": path, "tran": "usb", "model": (dev.get("model") or "").strip(), "status": "SKIPPED",
                          "timeout": False, "note": "disco USB: SMART non letto per non provocare reset"})
            continue
        text = run(["smartctl", "-H", "-A", "-i", path], timeout=30)
        if re.search(r"Unknown USB bridge|Please specify device type", text, re.I):
            text = run(["smartctl", "-d", "sat", "-H", "-A", "-i", path], timeout=30)
        info = parse_smart(text) if text else {"status": "UNKNOWN", "timeout": False}
        info.update(dev=path, tran=dev.get("tran") or "", model=info.get("model") or (dev.get("model") or "").strip())
        disks.append(info)
    return disks, now


# ---------------------------------------------------------------------------
# Processi più pesanti (campionamento di /proc, senza dipendenze)
# ---------------------------------------------------------------------------

def _sample_procs():
    out = {}
    for pid in os.listdir(PROC):
        if not pid.isdigit():
            continue
        stat = read(f"{PROC}/{pid}/stat")
        if ")" not in stat:
            continue
        comm = stat[stat.index("(") + 1:stat.rindex(")")]
        rest = stat[stat.rindex(")") + 2:].split()
        try:
            out[pid] = (comm, int(rest[11]) + int(rest[12]), int(rest[21]))
        except (IndexError, ValueError):
            continue
    return out


def top_processes(interval=1.0, sleep=time.sleep, n=5):
    clk = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    page = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
    a = _sample_procs()
    sleep(interval)
    b = _sample_procs()
    cpu, mem = {}, {}
    for pid, (comm, ticks, rss) in b.items():
        if pid in a:
            cpu[comm] = cpu.get(comm, 0.0) + max(0, ticks - a[pid][1]) / (clk * interval) * 100
        mem[comm] = mem.get(comm, 0) + rss * page / 1048576
    top = lambda d: [[k, round(v, 1)] for k, v in sorted(d.items(), key=lambda kv: -kv[1])[:n]]    # noqa: E731
    return {"cpu": top(cpu), "mem": top(mem)}


# ---------------------------------------------------------------------------
# Riepilogo dell'avvio («cosa è successo prima?»)
# ---------------------------------------------------------------------------

BOOT_LINE = re.compile(r"^\s*(-?\d+)\s+([0-9a-f]{32})\s+(.*)$")
LINUX_VERSION = re.compile(r"Linux version (\S+)")


def build_boot_report(mounts, docker, now, cur_events, prev=None):
    """Cosa è successo nell'avvio PRECEDENTE (come è finito, errori del kernel) e come è partito quello attuale."""
    bt = boot_ts()
    rep = {"generated_ts": now, "boot_ts": bt, "kernel_release": os.uname().release, "prev": {"found": False}}

    if prev is not None:                       # già calcolato: non rileggo il log enorme dell'avvio precedente
        rep["prev"] = prev
        boots = []
    else:
        boots = [m.group(1) for m in (BOOT_LINE.match(line) for line in run(["journalctl", "--list-boots", "--no-pager"]).splitlines()) if m]
        if "-1" in boots:
            first = first_lines(["journalctl", "-b", "-1", "-o", "short-unix", "--no-pager"], n=4)
            last = run(["journalctl", "-b", "-1", "-o", "short-unix", "-n", "40", "--no-pager"]).splitlines()
            ts = lambda line: float(line.split()[0]) if line and re.match(r"^\d+(\.\d+)?\s", line) else None    # noqa: E731
            start, end = (ts(first[0]) if first else None), (ts(last[-1]) if last else None)
            ver = next((m.group(1) for m in (LINUX_VERSION.search(x) for x in first) if m), None)
            events = kernel_events("-1")
            rep["prev"] = {
                "found": True, "start_ts": start, "end_ts": end,
                "duration_s": (end - start) if start and end else None,
                "clean_shutdown": any(CLEAN_SHUTDOWN.search(x) for x in last[-25:]),
                "kernel_release": ver,
                "last_lines": [re.sub(r"^\d+(\.\d+)?\s+", "", x)[:160] for x in last[-3:]],
                **kernel_summary(events),
            }

    rep["journal_persistent"] = os.path.isdir("/var/log/journal")
    rep["kernel_now"] = kernel_summary(cur_events)["counts"]
    rep["mounts"] = [{"path": m["path"], "mounted": m["mounted"], "delay_s": (m["since_ts"] - bt) if m.get("since_ts") else None}
                     for m in mounts]
    late = []
    for m in mounts:
        if not (m.get("mounted") and m.get("since_ts")):
            continue
        for c in docker:
            if c["started_ts"] and any(src.startswith(m["path"]) for src in c["mounts"]) and c["started_ts"] < m["since_ts"] - 5:
                late.append(c["name"])
    rep["containers_before_mount"] = sorted(set(late))
    return rep


# ---------------------------------------------------------------------------
# Programma principale
# ---------------------------------------------------------------------------

def load_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def atomic_write(path, data, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def main(now=None):
    now = now or time.time()
    state = load_state()
    boot_id = read(f"{PROC}/sys/kernel/random/boot_id")

    mounts, docker = collect_mounts(), collect_docker()
    events = kernel_events("0")
    smart, smart_ts = collect_smart(state, now)

    report = state.get("boot_report")
    new_boot = state.get("boot_id") != boot_id
    since_boot = now - boot_ts()
    if new_boot or (report and since_boot < BOOT_GRACE):
        report = build_boot_report(mounts, docker, now, events, prev=None if new_boot else (report or {}).get("prev"))
        report["boot_id"] = boot_id

    out = {
        "version": 1, "ts": now, "boot_id": boot_id, "boot_ts": boot_ts(), "kernel_release": os.uname().release,
        "hostname": os.uname().nodename, "mounts": mounts, "docker": docker, "kernel": kernel_summary(events),
        "smart": smart, "smart_ts": smart_ts, "top": top_processes(), "boot_report": report,
    }
    atomic_write(OUT, out)
    try:
        atomic_write(STATE, {"boot_id": boot_id, "boot_report": report, "smart": smart, "smart_ts": smart_ts}, 0o600)
    except OSError as e:
        print(f"Impossibile salvare lo stato della sonda in {STATE}: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
