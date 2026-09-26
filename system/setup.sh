#!/bin/bash
# One-time (idempotent) OS setup for PlatformGo on Raspberry Pi 3 A+ / DietPi.
# Run as root:  bash /opt/platformgo/system/setup.sh     (reboot afterwards)
set -euo pipefail

APP=platformgo
APP_DIR=/opt/$APP
DATA_DIR=/var/lib/$APP
ETC_DIR=/etc/$APP
REPO=${REPO:-https://github.com/mherec/PlatformGo_PI3_base.git}
DIETPI_HW=/boot/dietpi/func/dietpi-set_hardware

log()  { echo -e "\e[1;36m==> $*\e[0m"; }
warn() { echo -e "\e[1;33m!!  $*\e[0m"; }
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }

# --- boot files --------------------------------------------------------------
if   [ -f /boot/firmware/config.txt ]; then BOOT=/boot/firmware
elif [ -f /boot/config.txt ];          then BOOT=/boot
else echo "config.txt not found"; exit 1; fi
CONFIG=$BOOT/config.txt
CMDLINE=$BOOT/cmdline.txt
[ -f "$CONFIG.pg-orig" ]  || cp "$CONFIG"  "$CONFIG.pg-orig"
[ -f "$CMDLINE.pg-orig" ] || cp "$CMDLINE" "$CMDLINE.pg-orig"

# --- DietPi hardware helpers first (they keep dietpi-config consistent) -------
if [ -x "$DIETPI_HW" ]; then
  log "DietPi hardware settings"
  "$DIETPI_HW" serialconsole disable       || warn "serialconsole disable failed"
  "$DIETPI_HW" bluetooth enable            || warn "bluetooth enable failed"
  "$DIETPI_HW" soundcard rpi-bcm2835-3.5mm || warn "soundcard failed"
fi

# --- config.txt ----------------------------------------------------------------
log "config.txt: $CONFIG"
set_cfg() {  # set_cfg <regex matching existing lines> <final line>
  sed -i -E "/^[#[:space:]]*$1/d" "$CONFIG"
  echo "$2" >> "$CONFIG"
}
set_cfg 'enable_uart='      'enable_uart=1'
set_cfg 'dtparam=spi='      'dtparam=spi=on'
set_cfg 'dtparam=audio='    'dtparam=audio=on'
set_cfg 'core_freq='        'core_freq=250'
set_cfg 'boot_delay='       'boot_delay=0'
set_cfg 'disable_splash='   'disable_splash=1'
# Bluetooth must stay on PL011 (ttyAMA0), mini UART (ttyS0) stays ours
sed -i -E '/^[[:space:]]*dtoverlay=(pi3-)?(disable-bt|miniuart-bt)/d' "$CONFIG"

# --- cmdline.txt: no serial console, bigger SPI buffer for long LED strips -------
log "cmdline.txt: $CMDLINE"
sed -i -E 's/ ?console=(serial0|ttyS0|ttyAMA0)(,[0-9]+)?//g' "$CMDLINE"
grep -q 'spidev.bufsiz=' "$CMDLINE" || sed -i -E '1 s/$/ spidev.bufsiz=32768/' "$CMDLINE"
systemctl mask --now serial-getty@ttyS0.service serial-getty@ttyAMA0.service 2>/dev/null || true

# --- packages --------------------------------------------------------------------
log "APT packages"
apt-get update -q
apt-get install -y -q --no-install-recommends \
  git ca-certificates python3 python3-venv python3-pip \
  python3-aiohttp python3-serial python3-spidev python3-lgpio python3-gpiozero \
  alsa-utils bluez

# --- Bluetooth: LE only ----------------------------------------------------------
if [ -f /etc/bluetooth/main.conf ]; then
  sed -i -E '/^[#[:space:]]*ControllerMode[[:space:]]*=/d' /etc/bluetooth/main.conf
  sed -i -E 's/^\[General\]$/[General]\nControllerMode = le/' /etc/bluetooth/main.conf
fi
systemctl enable bluetooth 2>/dev/null || true
systemctl enable hciuart 2>/dev/null || true

# --- user, groups, device permissions ------------------------------------------------
log "user $APP"
for g in gpio spi dialout audio bluetooth; do getent group $g >/dev/null || groupadd -r $g; done
id $APP &>/dev/null || useradd -r -s /usr/sbin/nologin -d $DATA_DIR $APP
usermod -aG gpio,spi,dialout,audio,bluetooth $APP
install -m 644 "$(dirname "$0")/99-platformgo.rules" /etc/udev/rules.d/99-platformgo.rules
install -m 644 "$(dirname "$0")/platformgo-dbus.conf" /etc/dbus-1/system.d/platformgo.conf
udevadm control --reload && udevadm trigger || true

# --- app directories ---------------------------------------------------------------------
log "directories"
if [ ! -d $APP_DIR/.git ]; then git clone "$REPO" $APP_DIR; fi
install -d -o $APP -g $APP -m 750 $DATA_DIR
install -d -o root -g $APP -m 750 $ETC_DIR
[ -f $ETC_DIR/env ] || install -o root -g $APP -m 640 $APP_DIR/system/env.example $ETC_DIR/env

# --- DietPi boot speed -------------------------------------------------------------------
if [ -f /boot/dietpi.txt ]; then
  sed -i -E 's/^CONFIG_BOOT_WAIT_FOR_NETWORK=.*/CONFIG_BOOT_WAIT_FOR_NETWORK=0/' /boot/dietpi.txt
fi

# --- venv + service ----------------------------------------------------------------------
bash $APP_DIR/system/deploy.sh --no-pull
systemctl enable $APP

log "done - reboot required:  reboot"
