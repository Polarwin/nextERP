#!/usr/bin/env bash
set -euo pipefail

project_dir=/home/justin/Projects/nextERP
unit_name=nexterp.service
unit_source="$project_dir/$unit_name"
unit_target="/etc/systemd/system/$unit_name"

if [[ ! -x "$project_dir/bin/python" || ! -f "$project_dir/server.py" ]]; then
    echo "nextERP executable files are missing from $project_dir" >&2
    exit 1
fi

sudo install -m 0644 "$unit_source" "$unit_target"
sudo systemctl daemon-reload
sudo systemctl enable "$unit_name"

if sudo systemctl is-active --quiet "$unit_name"; then
    sudo systemctl restart "$unit_name"
    sudo systemctl --no-pager --full status "$unit_name"
    exit 0
fi

# Transition an existing manually-started nextERP instance without killing an
# unrelated service that happens to use the same port.
mapfile -t listener_pids < <(sudo lsof -t -iTCP:8347 -sTCP:LISTEN 2>/dev/null | sort -u)
for listener_pid in "${listener_pids[@]}"; do
    listener_cwd=$(sudo readlink -f "/proc/$listener_pid/cwd" 2>/dev/null || true)
    listener_cmd=$(sudo tr '\0' ' ' < "/proc/$listener_pid/cmdline" 2>/dev/null || true)
    if [[ "$listener_cwd" != "$project_dir" || "$listener_cmd" != *server.py* ]]; then
        echo "Refusing to stop unknown process $listener_pid on port 8347:" >&2
        echo "$listener_cmd" >&2
        echo "Stop it manually, then run: sudo systemctl start $unit_name" >&2
        exit 1
    fi
    echo "Stopping manually-started nextERP process $listener_pid"
    sudo kill -TERM "$listener_pid"
done

for _ in {1..30}; do
    if ! sudo lsof -t -iTCP:8347 -sTCP:LISTEN >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

if sudo lsof -t -iTCP:8347 -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Port 8347 did not become available; service was installed but not started." >&2
    exit 1
fi

sudo systemctl restart "$unit_name"
sudo systemctl --no-pager --full status "$unit_name"
