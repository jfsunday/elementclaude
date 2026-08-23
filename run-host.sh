#!/usr/bin/env bash
# Run elementclaude directly on the host (not inside Docker) so that !run can
# execute real host binaries — yay, pacman, sudo, anything in your PATH —
# and Claude can touch any directory you can read.
#
# Usage:
#   ./run-host.sh             # foreground, ctrl-c to stop
#   ./run-host.sh --install   # install + enable systemd user service, then exit
#   ./run-host.sh --logs      # tail systemd journal for the service
#   ./run-host.sh --stop      # stop service
#   ./run-host.sh --uninstall # disable + remove unit file
#
# Optional dependency extras (see pyproject `[project.optional-dependencies]`):
#   EXTRAS=voice ./run-host.sh            # STT/TTS support
#   EXTRAS="voice dev" ./run-host.sh      # several extras, space separated
#   EXTRAS=voice ./run-host.sh --install  # …baked into the systemd unit
# `uv run` re-syncs the venv on every start, so the extra has to be named here —
# a manual `uv pip install '.[voice]'` gets pruned away again on the next start.
#
# Prereqs:
#   - uv (https://docs.astral.sh/uv/) on PATH
#   - messaging-bot reachable on localhost (or wherever .env points)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_NAME="elementclaude.service"
UNIT_PATH="$UNIT_DIR/$UNIT_NAME"

EXTRAS="${EXTRAS:-}"
UV_EXTRA_ARGS=()
EXTRA_FLAGS=""
# Word-split on purpose so EXTRAS="voice dev" becomes two --extra flags.
for extra in $EXTRAS; do
    UV_EXTRA_ARGS+=(--extra "$extra")
    EXTRA_FLAGS+="--extra $extra "
done

cmd_install() {
    mkdir -p "$UNIT_DIR" "$ROOT/data"
    cat >"$UNIT_PATH" <<EOF
[Unit]
Description=elementclaude — Claude Code in an Element room
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$ROOT
ExecStart=$(command -v uv) run ${EXTRA_FLAGS}--project $ROOT uvicorn app.main:app --host 0.0.0.0 --port 7075
Restart=on-failure
RestartSec=3
# Inherit user env (PATH, ANTHROPIC_BASE_URL from shell, etc.)
PassEnvironment=PATH HOME LANG TERM

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now "$UNIT_NAME"
    echo "installed and started — tail logs with: ./run-host.sh --logs"
}

cmd_logs()     { journalctl --user -u "$UNIT_NAME" -f; }
cmd_stop()     { systemctl --user stop "$UNIT_NAME"; }
cmd_uninstall(){ systemctl --user disable --now "$UNIT_NAME" || true; rm -f "$UNIT_PATH"; systemctl --user daemon-reload; echo "removed"; }

cmd_run() {
    exec uv run ${UV_EXTRA_ARGS[@]+"${UV_EXTRA_ARGS[@]}"} --project "$ROOT" \
        uvicorn app.main:app --host 0.0.0.0 --port 7075
}

case "${1:-run}" in
    --install)   cmd_install ;;
    --logs)      cmd_logs ;;
    --stop)      cmd_stop ;;
    --uninstall) cmd_uninstall ;;
    run|"")      cmd_run ;;
    *)           echo "unknown arg: $1"; exit 2 ;;
esac
