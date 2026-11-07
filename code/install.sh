#!/usr/bin/env bash
# Tesseract installer for a Raspberry Pi Zero 2 W running Raspberry Pi OS Lite (64-bit).
#   cd tesseract/code && sudo ./install.sh
# Safe to re-run (e.g. after changing pins in config.toml).
set -euo pipefail

CODE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$CODE_DIR/.venv"
BOOT=/boot/firmware
[ -d "$BOOT" ] || BOOT=/boot
CONFIG_TXT="$BOOT/config.txt"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31m%s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run with sudo: sudo ./install.sh"
[ "$(uname -m)" = "aarch64" ] || die "Needs 64-bit Raspberry Pi OS (onnxruntime has no 32-bit ARM builds). Re-flash with 'Raspberry Pi OS Lite (64-bit)'."
[ -f "$CONFIG_TXT" ] || die "Can't find $CONFIG_TXT. Is this a Raspberry Pi?"

# Read a value from [pins] in config.toml (python3 has tomllib on Pi OS bookworm and later)
pin() { python3 -c "import tomllib,sys; print(tomllib.load(open('$CODE_DIR/config.toml','rb'))['pins'].get('$1', $2))"; }
AMP_GPIO=$(pin amp_enable 0)                      # the amp's SD_MODE, if it's wired to a GPIO...
[ "$AMP_GPIO" != "0" ] || AMP_GPIO=$(pin amp_enable_dummy 25)   # ...else a pin with nothing on it
CS_GPIO=$(pin display_cs 8)

say "Installing system packages"
apt-get update -qq
apt-get install -y -qq python3-venv python3-dev build-essential libasound2-dev alsa-utils \
    device-tree-compiler fonts-dejavu-core python3-gpiozero python3-lgpio python3-spidev iw dnsmasq-base

say "Building the sound card overlay (amp-enable dummy pin GPIO$AMP_GPIO)"
dtc -@ -q -I dts -O dtb -o "$BOOT/overlays/tesseract-audio.dtbo" "$CODE_DIR/boot/tesseract-audio.dts" \
    || die "Overlay failed to compile"

say "Configuring $CONFIG_TXT"
cp "$CONFIG_TXT" "$CONFIG_TXT.bak-tesseract"
# The on-board audio driver claims the PWM block the LED strip needs.
sed -i 's/^dtparam=audio=on/#dtparam=audio=on  # disabled by tesseract/' "$CONFIG_TXT"
sed -i '/^# >>> tesseract/,/^# <<< tesseract/d' "$CONFIG_TXT"
cat >> "$CONFIG_TXT" <<EOF
# >>> tesseract (managed by code/install.sh; edits here are overwritten)
[all]
dtparam=spi=on
# one chip-select only (the display), on [pins] display_cs; GPIO7 stays a normal GPIO
dtoverlay=spi0-1cs,cs0_pin=$CS_GPIO
dtparam=audio=off
dtoverlay=tesseract-audio,amp_gpio=$AMP_GPIO
# start the kernel straight away (no rainbow screen, no pause): the boot light comes on sooner
disable_splash=1
boot_delay=0
# <<< tesseract
EOF

say "Turning off Wi-Fi power saving (it adds 100+ ms of latency to every request)"
if [ -d /etc/NetworkManager/conf.d ]; then
    printf '[connection]\nwifi.powersave = 2\n' > /etc/NetworkManager/conf.d/tesseract-wifi-powersave.conf
fi
iw dev wlan0 set power_save off 2>/dev/null || true

say "Keeping logs across reboots (capped at 100 MB), so a problem from before a reboot can still be read"
# Raspberry Pi OS keeps the journal in RAM only; this drop-in sorts after its 40-rpi-volatile one, so it wins
mkdir -p /var/log/journal /etc/systemd/journald.conf.d
printf '[Journal]\nStorage=persistent\nSystemMaxUse=100M\n' > /etc/systemd/journald.conf.d/tesseract.conf
systemctl restart systemd-journald 2>/dev/null || true

say "Setting up the Wi-Fi setup hotspot"
# While the setup hotspot is up, every DNS name points at Tesseract, so a phone's
# "is there internet?" check gets the setup page and pops it up by itself.
mkdir -p /etc/NetworkManager/dnsmasq-shared.d
echo "address=/#/10.42.0.1" > /etc/NetworkManager/dnsmasq-shared.d/tesseract-setup.conf

say "Creating Python environment"
[ -d "$VENV" ] || python3 -m venv --system-site-packages "$VENV"
"$VENV/bin/pip" install -q --upgrade pip wheel
"$VENV/bin/pip" install -q -r "$CODE_DIR/requirements.txt"

say "Downloading wake word and speech detection models"
cd "$CODE_DIR"
"$VENV/bin/python" -c "
from tesseract import config
from tesseract.vad import ensure_silero
from tesseract.wakeword import FEATURE_MODELS, PRETRAINED, ensure_kws_model, ensure_model_files
cfg = config.load()
ensure_model_files(cfg.path('models'), [*FEATURE_MODELS, PRETRAINED['hey_jarvis']])
ensure_kws_model(cfg.path('models'))
ensure_silero(cfg.path('models'))
"

if [ ! -f "$CODE_DIR/.env" ] || ! grep -q '^OPENROUTER_API_KEY=.\+' "$CODE_DIR/.env"; then
    [ -f "$CODE_DIR/.env" ] || cp "$CODE_DIR/.env.example" "$CODE_DIR/.env"
    if [ -t 0 ]; then
        read -rsp "OpenRouter API key (https://openrouter.ai/keys; hidden as you type), or Enter to skip: " KEY
        echo
        if [ -n "$KEY" ]; then
            if grep -q '^OPENROUTER_API_KEY=' "$CODE_DIR/.env"; then
                sed -i "s|^OPENROUTER_API_KEY=.*|OPENROUTER_API_KEY=$KEY|" "$CODE_DIR/.env"
            else    # e.g. an empty .env copied over from a laptop
                echo "OPENROUTER_API_KEY=$KEY" >> "$CODE_DIR/.env"
            fi
        fi
    fi
fi
chmod 600 "$CODE_DIR/.env"

say "Adding the 'tesseract' command"
cat > /usr/local/bin/tesseract <<WRAPPER
#!/bin/sh
# Tesseract's command line, from anywhere: tesseract doctor | wifi add "Home" | location "Chicago" | ...
# Runs as root because the LEDs, Wi-Fi and the clock's time zone need it.
[ "\$(id -u)" -eq 0 ] || exec sudo "\$0" "\$@"
cd "$CODE_DIR" && exec "$VENV/bin/python" -m tesseract "\$@"
WRAPPER
chmod 755 /usr/local/bin/tesseract

say "Installing the systemd service"
for unit in tesseract.service tesseract-splash.service; do
    sed -e "s|@CODE_DIR@|$CODE_DIR|g" -e "s|@VENV@|$VENV|g" "$CODE_DIR/systemd/$unit" > "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable tesseract.service tesseract-splash.service

say "Done"
cat <<EOF
  1. Set your timezone if you haven't:   sudo raspi-config  (Localisation Options > Timezone)
  2. Reboot so the sound card and SPI come up:   sudo reboot
  3. After the reboot, stop the service while you test:   sudo systemctl disable --now tesseract
  4. Check everything:   tesseract doctor
  5. Bring-up tests:   tesseract display | leds | buttons | record | say "hello" | wake   (BRINGUP.md)
  6. Then turn it back on:   sudo systemctl enable --now tesseract    Logs:   journalctl -u tesseract -f
EOF
