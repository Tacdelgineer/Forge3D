#!/usr/bin/env bash
# Install (or update) the Forge3D host resource-control helper.
#
#     sudo host/install.sh
#
# Installs one binary, one systemd unit and one tmpfiles rule. Uninstall with
#     sudo host/install.sh --uninstall
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN=/usr/local/sbin/forge3d-resctl
UNIT=/etc/systemd/system/forge3d-resctl.service
TMPF=/etc/tmpfiles.d/forge3d-resctl.conf

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }

if [[ "${1:-}" == "--uninstall" ]]; then
    # Restore anything still paused BEFORE removing the helper that knows how.
    if [[ -x $BIN ]]; then "$BIN" recover || true; fi
    systemctl disable --now forge3d-resctl.service 2>/dev/null || true
    rm -f "$BIN" "$UNIT" "$TMPF"
    systemctl daemon-reload
    echo "forge3d-resctl removed (state kept at /var/lib/forge3d-resctl)"
    exit 0
fi

install -m 0755 -o root -g root "$HERE/forge3d-resctl" "$BIN"
install -m 0644 -o root -g root "$HERE/forge3d-resctl.service" "$UNIT"
install -m 0644 -o root -g root "$HERE/forge3d-resctl.tmpfiles.conf" "$TMPF"

systemd-tmpfiles --create "$TMPF"
systemctl daemon-reload
systemctl enable --now forge3d-resctl.service
sleep 1
systemctl --no-pager --lines=5 status forge3d-resctl.service || true
echo
echo "socket:"; ls -l /run/forge3d-resctl/resctl.sock
