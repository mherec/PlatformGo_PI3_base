#!/bin/bash
# Update code from GitHub and restart the service.  Run as root on the Pi:
#   bash /opt/platformgo/system/deploy.sh            (pull origin/main + restart)
#   bash /opt/platformgo/system/deploy.sh --no-pull  (only venv/service refresh)
set -euo pipefail
APP=platformgo
APP_DIR=/opt/$APP
BRANCH=${BRANCH:-main}
cd $APP_DIR

if [ "${1:-}" != "--no-pull" ]; then
  git fetch --quiet origin "$BRANCH"
  git reset --hard --quiet "origin/$BRANCH"
fi
echo "==> $(git log -1 --format='%h %s')"

# venv sees apt packages (lgpio, spidev, gpiozero...) -> no compiling on the Pi
[ -x .venv/bin/python ] || python3 -m venv --system-site-packages .venv
REQ_HASH=$(sha256sum requirements.txt | cut -d' ' -f1)
if [ "$(cat .venv/.req-hash 2>/dev/null)" != "$REQ_HASH" ]; then
  .venv/bin/pip install --quiet --disable-pip-version-check -r requirements.txt
  echo "$REQ_HASH" > .venv/.req-hash
fi

if ! cmp -s system/$APP.service /etc/systemd/system/$APP.service; then
  install -m 644 system/$APP.service /etc/systemd/system/$APP.service
  systemctl daemon-reload
fi
systemctl restart $APP
sleep 2
systemctl --no-pager --lines=5 status $APP || true
