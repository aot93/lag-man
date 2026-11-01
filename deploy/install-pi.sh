#!/bin/sh
# Start the lag-man generator at boot on a Raspberry Pi, and let it change the network.
# Run from the repo, as the user who will run the generator (it uses sudo):
#     deploy/install-pi.sh
set -eu

REPO=$(cd "$(dirname "$0")/.." && pwd)
RUN_USER=$(id -un)
UNIT=/etc/systemd/system/lagman-generator.service
DEFAULTS=/etc/default/lagman-generator
POLKIT=/etc/polkit-1/rules.d/50-lagman-network.rules

if [ "$RUN_USER" = root ]; then
    echo "Run this as the user who runs the generator, not as root (it uses sudo)." >&2
    exit 1
fi
if [ ! -x "$REPO/.venv/bin/lagman-generator" ]; then
    echo "No $REPO/.venv/bin/lagman-generator: run 'uv sync --extra generator' first." >&2
    exit 1
fi

echo "Installing $UNIT (user $RUN_USER, repo $REPO)"
sed -e "s|@USER@|$RUN_USER|g" -e "s|@REPO@|$REPO|g" "$REPO/deploy/lagman-generator.service" \
    | sudo tee "$UNIT" >/dev/null

if [ ! -e "$DEFAULTS" ]; then
    echo "Creating $DEFAULTS"
    sudo tee "$DEFAULTS" >/dev/null <<'CONF'
# Settings for the lag-man generator service. After editing:
#     sudo systemctl restart lagman-generator
# Web UI password (any user name is accepted). Blank: no password.
LAGMAN_PASSWORD=
# Extra command-line options, for example: --mode 1920x1080@50 --rate-set 50
LAGMAN_ARGS=
CONF
    sudo chmod 600 "$DEFAULTS"
fi

# NetworkManager lets only a logged-in user change the network; a service has no login,
# so allow this user explicitly.
echo "Installing $POLKIT"
sudo tee "$POLKIT" >/dev/null <<RULES
// Lets the lag-man generator (user $RUN_USER) change the network from its web UI.
polkit.addRule(function (action, subject) {
    if (action.id.indexOf("org.freedesktop.NetworkManager.") === 0 &&
            subject.user === "$RUN_USER") {
        return polkit.Result.YES;
    }
});
RULES

sudo systemctl daemon-reload
sudo systemctl enable lagman-generator
sudo systemctl restart lagman-generator
echo
echo "Started. Logs: journalctl -u lagman-generator -f"
echo "Stop it before running the generator by hand: sudo systemctl stop lagman-generator"
