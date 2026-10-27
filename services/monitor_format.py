"""Testi (HTML di Telegram) per il monitoraggio del server. Nessuna lettura di sistema: solo formattazione."""
import html
import os
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Rome")
KIND_LABELS = {"usb": "USB", "io": "errori disco", "fs": "filesystem", "power": "tensione"}


def esc(text) -> str:
    return html.escape(str(text), quote=False)


def fmt_bytes(n) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit, div in (("TB", 1 << 40), ("GB", 1 << 30), ("MB", 1 << 20)):
        if n >= div:
            return f"{n / div:.1f} {unit}" if unit != "MB" else f"{n / div:.0f} MB"
    return f"{n / 1024:.0f} KB"


def fmt_uptime(seconds) -> str:
    s = int(seconds or 0)
    d, rest = divmod(s, 86400)
    h, rest = divmod(rest, 3600)
    m, sec = divmod(rest, 60)
    if d:
        return f"{d} giorni {h} h" if d > 1 else f"1 giorno {h} h"
    if h:
        return f"{h} h {m:02d} min"
    return f"{m} min {sec:02d} s" if m < 10 else f"{m} min"


def fmt_dt(ts) -> str:
    return datetime.fromtimestamp(float(ts), TZ).strftime("%d/%m %H:%M")


def fmt_counts(counts: dict) -> str:
    parts = [f"{n} {KIND_LABELS.get(k, k)}" for k, n in counts.items() if n]
    return ", ".join(parts) if parts else "nessuno"


# ---------------------------------------------------------------------------
# Avvisi
# ---------------------------------------------------------------------------

def alert_text(kind: str, check) -> str:
    if kind == "resolve":
        return check.ok_text
    if kind == "remind":
        return "🔁 <b>Ancora aperto</b>\n" + check.text
    return check.text


def kernel_alert_text(delta: dict, events: list) -> str:
    lines = ["🚨 <b>Errori del kernel</b> nuovi: " + fmt_counts(delta)]
    for e in events:
        lines.append(f"<code>{esc(e['text'][:110])}</code> <i>({fmt_dt(e['ts'])})</i>")
    if delta.get("usb"):
        lines.append("Se si ripete: cavo, porta o alimentazione dell'SSD USB.")
    return "\n".join(lines)


def mute_text(hours: float, until: float) -> str:
    h = f"{hours:g}"
    return f"🔕 Avvisi silenziati per {h} {'ora' if hours == 1 else 'ore'} (fino alle {datetime.fromtimestamp(until, TZ).strftime('%H:%M')}). " \
           "<code>/silenzia 0</code> per riattivarli."


def unmute_text(active: list) -> str:
    if not active:
        return "🔔 Silenzio finito: nessun problema attivo."
    return "🔔 <b>Silenzio finito.</b> Ancora attivi:\n" + "\n".join(f"• {text}" for _k, _lvl, text in active if text)


# ---------------------------------------------------------------------------
# Riepilogo dell'avvio
# ---------------------------------------------------------------------------

def boot_basic_text(snap: dict) -> str:
    return (f"🔄 <b>Il server si è riavviato</b> (acceso da {fmt_uptime(snap['uptime_s'])}).\n"
            "Per sapere cosa è successo prima serve la sonda dell'host: <code>sudo bash monitor/install_monitor.sh</code>")


def boot_report_text(rep: dict, probe, snap: dict) -> str:
    L = [f"🔄 <b>Il server si è riavviato</b> · {fmt_dt(rep['boot_ts'])}"]
    prev = rep.get("prev") or {}
    if prev.get("found"):
        dur = fmt_uptime(prev["duration_s"]) if prev.get("duration_s") else "?"
        L.append(f"\n<b>Avvio precedente</b> (durato {dur})")
        if prev.get("clean_shutdown"):
            L.append("✅ Finito con uno spegnimento pulito.")
        else:
            L.append("❌ <b>Non è finito con uno spegnimento pulito</b>: possibile blackout, blocco del sistema o spegnimento forzato.")
            for line in (prev.get("last_lines") or [])[-2:]:
                L.append(f"<code>{esc(line[:110])}</code>")
        if prev.get("kernel_release") and prev["kernel_release"] != rep.get("kernel_release"):
            L.append(f"🔧 Kernel aggiornato: {esc(prev['kernel_release'])} → {esc(rep.get('kernel_release', '?'))}")
        counts, evs = prev.get("counts") or {}, prev.get("events") or []
        if sum(counts.values()):
            span = f" (dal {fmt_dt(evs[0]['ts'])} al {fmt_dt(evs[-1]['ts'])})" if evs else ""
            L.append(f"⚠️ <b>Errori del kernel in quell'avvio</b>: {fmt_counts(counts)}{span}")
            for e in evs[-3:]:
                L.append(f"<code>{esc(e['text'][:110])}</code>")
        else:
            L.append("✅ Nessun errore USB/disco/filesystem nei log di quell'avvio.")
    else:
        L.append("\nℹ️ Il log dell'avvio precedente non è disponibile"
                 + ("" if rep.get("journal_persistent") else
                    " (il journal non è persistente: <code>sudo mkdir -p /var/log/journal &amp;&amp; sudo systemctl restart systemd-journald</code>)") + ".")

    L.append("\n<b>Questo avvio</b>")
    for m in rep.get("mounts") or []:
        if not m.get("mounted"):
            L.append(f"❌ <b>{esc(m['path'])} NON è montato</b>")
        elif m.get("delay_s") is None:
            L.append(f"✅ {esc(m['path'])} montato")
        elif m["delay_s"] > 180:
            L.append(f"⚠️ {esc(m['path'])} montato dopo {fmt_uptime(m['delay_s'])}: tardi, i servizi partiti prima hanno visto una cartella vuota")
        else:
            L.append(f"✅ {esc(m['path'])} montato dopo {fmt_uptime(m['delay_s'])}")
    late = rep.get("containers_before_mount") or []
    if late:
        L.append("❌ Container partiti PRIMA del montaggio: " + ", ".join(esc(n) for n in late)
                 + ". Riavviali: <code>docker restart " + " ".join(esc(n) for n in late) + "</code>")
    now_counts = rep.get("kernel_now") or {}
    L.append(f"⚠️ Errori del kernel in questo avvio: {fmt_counts(now_counts)}" if sum(now_counts.values())
             else "✅ Nessun errore del kernel finora.")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# /server
# ---------------------------------------------------------------------------

def _ok(flag) -> str:
    return "✅" if flag else "❌"


def server_report(snap: dict, probe, probe_age, nc, avg_cpu, peak_temp, active_alerts: list, now: float) -> str:
    L = []
    host = (probe or {}).get("hostname") or os.uname().nodename
    L.append(f"🖥️ <b>Server</b> · {esc(host)}")
    L.append(f"⏱️ Acceso da {fmt_uptime(snap['uptime_s'])} · dal {fmt_dt(now - snap['uptime_s'])}")

    cpu = snap.get("cpu_pct")
    line = "🧠 <b>CPU</b> " + (f"{cpu:.0f}%" if cpu is not None else "n/d")
    if avg_cpu is not None:
        line += f" (media {avg_cpu:.0f}%)"
    line += f" · {snap['cores']} core"
    if snap.get("load"):
        line += " · carico " + " ".join(f"{x:.2f}" for x in snap["load"])
    L.append(line)

    t = snap.get("cpu_temp")
    parts = []
    if t is not None:
        parts.append(f"CPU {t:.0f}°C" + (f" (picco {peak_temp:.0f}°C nell'ultima ora)" if peak_temp and peak_temp > t + 1 else ""))
    if snap.get("fans"):
        parts.append("ventola " + " / ".join(f"{f['rpm']} giri" for f in snap["fans"][:2]))
    disks_t = [f"{os.path.basename(d['dev'])} {d['temp']}°C" for d in (probe or {}).get("smart", []) if d.get("temp") is not None]
    if disks_t:
        parts.append("dischi " + ", ".join(disks_t) + (f" (SMART delle {fmt_dt(probe['smart_ts'])[-5:]})" if probe.get("smart_ts") else ""))
    if parts:
        L.append("🌡️ <b>Temperature</b> " + " · ".join(parts))

    m = snap["mem"]
    ram = f"💾 <b>RAM</b> {fmt_bytes(m['used'])} / {fmt_bytes(m['total'])}" + (f" ({m['pct_used']:.0f}%)" if m.get("pct_used") is not None else "")
    if m.get("swap_total"):
        ram += f" · swap {fmt_bytes(m['swap_used'])}"
    L.append(ram)

    disks = []
    if snap.get("root"):
        r = snap["root"]
        disks.append(f"/ {fmt_bytes(r['used'])} / {fmt_bytes(r['total'])} ({r['pct']:.0f}%)")
    for mt in (probe or {}).get("mounts", []):
        if mt.get("mounted"):
            pct = f" ({100.0 * mt['used'] / mt['total']:.0f}%)" if mt.get("total") else ""
            used = f"{fmt_bytes(mt.get('used'))} / {fmt_bytes(mt.get('total'))}{pct} " if mt.get("total") else ""
            src = os.path.basename(mt.get("source", ""))
            disks.append(f"{esc(mt['path'])} {used}✅ montato ({esc(src)}, {esc(mt.get('fstype', ''))}, {'SOLA LETTURA' if mt.get('ro') else 'rw'})")
        else:
            disks.append(f"{esc(mt['path'])} ❌ <b>NON montato</b>")
    if disks:
        L.append("💽 <b>Dischi</b> " + " · ".join(disks))

    pw = snap.get("power") or {}
    if pw.get("ac") is not None or pw.get("battery"):
        s = "🔌 <b>Corrente</b> " + ("rete ✅" if pw.get("ac") else "❌ <b>assente</b>" if pw.get("ac") is False else "n/d")
        if pw.get("battery") and pw["battery"].get("pct") is not None:
            s += f" · batteria {pw['battery']['pct']}% ({esc(pw['battery'].get('status', ''))})"
        L.append(s)

    if snap.get("net"):
        L.append("🌐 <b>Rete</b> " + " · ".join(f"{esc(i['name'])} {_ok(i['up'])}" for i in snap["net"]))

    if nc is not None:
        ok, why, ms = nc
        L.append("☁️ <b>Nextcloud</b> " + (f"✅ risponde{f' ({ms} ms)' if ms else ''}" if ok else f"❌ {esc(why)}"))

    if probe:
        dock = probe.get("docker") or []
        if dock:
            L.append("🐳 <b>Docker</b> " + " · ".join(f"{_ok(c['state'] == 'running')} {esc(c['name'])}" + ("" if c["state"] == "running" else f" ({esc(c['state'])})") for c in dock))
        counts = (probe.get("kernel") or {}).get("counts") or {}
        L.append("🛠️ <b>Kernel</b> " + (f"⚠️ dal riavvio: {fmt_counts(counts)}" if sum(counts.values()) else "✅ nessun errore dal riavvio"))
        smart = []
        for d in probe.get("smart") or []:
            name = os.path.basename(d.get("dev", "?"))
            if d.get("status") == "SKIPPED":
                smart.append(f"{name} ⏸️ (USB: non letto per non causare reset)")
                continue
            ok_ = d.get("status") == "PASSED" and not d.get("timeout")
            smart.append(f"{name} {'✅' if ok_ else '❌' if d.get('status') == 'FAILED' else '⚠️'}"
                         + (" (timeout USB)" if d.get("timeout") else ""))
        if smart:
            L.append("🩺 <b>SMART</b> " + " · ".join(smart))
        top = probe.get("top") or {}
        if top.get("cpu"):
            L.append("📊 <b>Processi</b> CPU: " + ", ".join(f"{esc(n)} {v:.0f}%" for n, v in top["cpu"][:3])
                     + " · RAM: " + ", ".join(f"{esc(n)} {v:.0f} MB" for n, v in (top.get("mem") or [])[:3]))
    if snap.get("bot_mem"):
        L.append(f"🤖 <b>EDITH</b> usa {fmt_bytes(snap['bot_mem'])} di RAM")

    if probe is None:
        L.append("\nℹ️ La sonda dell'host non è attiva: mancano montaggi, Docker, log del kernel e SMART (<code>sudo bash monitor/install_monitor.sh</code>).")
    elif probe_age and probe_age > 300:
        L.append(f"\n⚠️ La sonda dell'host non aggiorna da {int(probe_age / 60)} minuti: questi dati possono essere vecchi.")

    if active_alerts:
        L.append("\n🔔 <b>Avvisi attivi</b>")
        L += [f"• {text}" for _k, _lvl, text in active_alerts if text]
    return "\n".join(L)
