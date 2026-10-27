#!/usr/bin/env bash
# Installa la sonda EDITH come timer systemd (un controllo al minuto).
#
#   sudo bash monitor/install_monitor.sh              installa e avvia
#   sudo bash monitor/install_monitor.sh --uninstall  toglie tutto
#
# La sonda è in SOLA LETTURA: legge log del kernel, SMART, montaggi e Docker e scrive solo
# data/host_status.json (+ un piccolo stato in /var/lib/edith-probe).
# Opzioni (facoltative) in /etc/default/edith-probe, ad esempio:  PROBE_MOUNTS=/mnt/data,/mnt/altro
#
# Solo per prove: EDITH_UNIT_DIR=cartella  EDITH_NO_SYSTEMCTL=1

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROBE="$DIR/monitor/host_probe.py"
UNIT_DIR="${EDITH_UNIT_DIR:-/etc/systemd/system}"
SYSTEMCTL="systemctl"; [ "${EDITH_NO_SYSTEMCTL:-0}" = "1" ] && SYSTEMCTL="true"

if [ "${EDITH_NO_SYSTEMCTL:-0}" != "1" ] && [ "$(id -u)" -ne 0 ]; then
  echo "Serve sudo:  sudo bash monitor/install_monitor.sh" >&2
  exit 1
fi

if [ "${1:-}" = "--uninstall" ]; then
  $SYSTEMCTL disable --now edith-probe.timer 2>/dev/null || true
  rm -f "$UNIT_DIR/edith-probe.service" "$UNIT_DIR/edith-probe.timer"
  $SYSTEMCTL daemon-reload
  echo "Sonda rimossa. (Restano data/host_status.json e /var/lib/edith-probe: puoi cancellarli.)"
  exit 0
fi

command -v python3 >/dev/null 2>&1 || { echo "python3 non trovato" >&2; exit 1; }
[ -f "$PROBE" ] || { echo "Non trovo $PROBE" >&2; exit 1; }
mkdir -p "$DIR/data" /var/lib/edith-probe 2>/dev/null || true
chmod 700 /var/lib/edith-probe 2>/dev/null || true
mkdir -p "$UNIT_DIR"

cat > "$UNIT_DIR/edith-probe.service" <<EOF
[Unit]
Description=EDITH: sonda dell'host (raccoglie dati per il monitoraggio)

[Service]
Type=oneshot
EnvironmentFile=-/etc/default/edith-probe
ExecStart=/usr/bin/env python3 $PROBE
Nice=10
IOSchedulingClass=idle
EOF

cat > "$UNIT_DIR/edith-probe.timer" <<EOF
[Unit]
Description=EDITH: sonda dell'host, un controllo al minuto

[Timer]
OnBootSec=45s
OnUnitActiveSec=60s
AccuracySec=5s

[Install]
WantedBy=timers.target
EOF

$SYSTEMCTL daemon-reload
$SYSTEMCTL enable --now edith-probe.timer
$SYSTEMCTL start edith-probe.service || true      # primo giro subito

echo "Sonda installata."
echo "  Controlla:   systemctl list-timers edith-probe.timer"
echo "  Ultimo giro: journalctl -u edith-probe.service -n 5 --no-pager"
echo "  File dati:   $DIR/data/host_status.json"
