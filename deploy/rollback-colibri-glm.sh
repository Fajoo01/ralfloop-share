#!/usr/bin/env bash
set -euo pipefail

if (( $# != 1 )); then
    echo "usage: $0 /home/sibilla-cumana/ralfloop-production/releases/COMMIT" >&2
    exit 64
fi

production=/home/sibilla-cumana/ralfloop-production
previous=$(realpath "$1")
case "$previous" in
    "$production"/releases/*) ;;
    *) echo "rollback target outside production releases" >&2; exit 64 ;;
esac
if [[ ! -d $previous || ! -r $previous/pyproject.toml ]]; then
    echo "rollback target is not a Ralfloop release" >&2
    exit 66
fi

uid=$(id -u sibilla-cumana)
runtime=/run/user/$uid
user_systemctl=(runuser -u sibilla-cumana -- env XDG_RUNTIME_DIR="$runtime" DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime/bus" systemctl --user)
"${user_systemctl[@]}" stop ralf-glm-worker.timer ralf-glm-digest.timer || true
for unit in \
    ralf-bandi-weekly-deep-research.service \
    ralf-glm-worker.service ralf-glm-worker.timer \
    ralf-glm-digest.service ralf-glm-digest.timer
do
    source=$previous/deploy/systemd/user/$unit
    if [[ -f $source ]]; then
        install -o sibilla-cumana -g sibilla-cumana -m 0644 \
            "$source" "/home/sibilla-cumana/.config/systemd/user/$unit"
    fi
done
"${user_systemctl[@]}" daemon-reload

temporary=$production/.current.rollback-$$
ln -s "$previous" "$temporary"
mv -Tf "$temporary" "$production/current"
systemctl restart ralfloop-backend.service
if [[ -f $previous/deploy/systemd/user/ralf-glm-worker.timer ]]; then
    "${user_systemctl[@]}" enable --now ralf-glm-worker.timer ralf-glm-digest.timer
fi
systemctl is-active ralfloop-backend.service
systemctl is-active meowgram.service
backend_ready=0
for attempt in {1..20}; do
    if curl -fsS --max-time 2 http://127.0.0.1:19090/openapi.json >/dev/null; then
        backend_ready=1
        break
    fi
    sleep 1
done
if (( backend_ready == 0 )); then
    echo "backend health timeout after rollback" >&2
    exit 1
fi
curl -fsS --max-time 5 http://127.0.0.1:19093/health
echo "rollback complete; GLM artifacts preserved"
