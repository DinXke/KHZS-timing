#!/bin/bash
# Bouwt het KHZS Timing-image voor de Raspberry Pi (3B en nieuwer, 64-bit) met pi-gen in Docker.
# Starten in WSL:  bash /mnt/c/Users/scheb/alge-livetiming/appliance/build-image.sh
# Resultaat:       dist/khzs-timing-<versie>.img.xz  (flashen met Raspberry Pi Imager of appliance/flash-sd.ps1)
set -euo pipefail
exec > >(tee "$HOME/khzs-build.log") 2>&1

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PIGEN="${PIGEN:-$HOME/pi-gen}"
SECRETS="$REPO/appliance/secrets.env"

[ -d "$PIGEN" ] || git clone -b arm64 https://github.com/RPi-Distro/pi-gen.git "$PIGEN"
git -C "$PIGEN" checkout -q arm64

# ---- noodlogin (SSH/serieel) voor gebruiker khzs: eenmalig willekeurig wachtwoord, nooit in git
if [ ! -f "$SECRETS" ]; then
	PW="$(python3 -c "import secrets;print(''.join(secrets.choice('abcdefghjkmnpqrstuvwxyz23456789') for _ in range(12)))")"
	printf '# Noodlogin op de Pi (SSH of seriële console). NIET in git.\nPI_USER=khzs\nPI_PASSWORD=%s\n' "$PW" > "$SECRETS"
fi
PI_PASSWORD="$(grep '^PI_PASSWORD=' "$SECRETS" | cut -d= -f2-)"
# PUBLIEK=1: image voor GitHub – zonder thuis-Wi-Fi, met een eigen noodwachtwoord (enkel in secrets.env)
SUFFIX=""
if [ "${PUBLIEK:-0}" = 1 ]; then
	if ! grep -q '^PUBLIC_PI_PASSWORD=' "$SECRETS"; then
		printf 'PUBLIC_PI_PASSWORD=%s\n' "$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')" >> "$SECRETS"
	fi
	PI_PASSWORD="$(grep '^PUBLIC_PI_PASSWORD=' "$SECRETS" | cut -d= -f2- | tr -d '\r')"
	SUFFIX="-publiek"
fi

# ---- eigen stap klaarzetten: app-release + systeemdienst + configuratie
ST="$PIGEN/stage-khzs"
rm -rf "$ST"
cp -r "$REPO/appliance/pi-gen/stage-khzs" "$ST"
F="$ST/00-khzs/files"
mkdir -p "$F"
python3 "$REPO/tools/build_release.py" --dir "$F"
cp -r "$REPO/appliance/agent" "$REPO/appliance/systemd" "$REPO/appliance/conf" "$F/"
find "$ST" -type f \( -name '*.sh' -o -name '*-packages' -o -name 'EXPORT_IMAGE' -o -name '*.service' -o -name '*.conf' -o -name '*.json' -o -name '*.py' -o -name motd \) -exec sed -i 's/\r$//' {} +
chmod +x "$ST/prerun.sh" "$ST/00-khzs/01-run.sh"
# standaard Wi-Fi (uit secrets.env, niet in git) als bewaard netwerk in het image
WSSID="$(grep '^HOME_WIFI_SSID=' "$SECRETS" | cut -d= -f2- | tr -d '
')"
WPSK="$(grep '^HOME_WIFI_PSK=' "$SECRETS" | cut -d= -f2- | tr -d '
')"
if [ -n "$WSSID" ] && [ "${PUBLIEK:-0}" != 1 ]; then
	python3 - "$WSSID" "$WPSK" "$F/wifi-home.nmconnection" <<'PYW'
import sys, uuid
ssid, psk, out = sys.argv[1], sys.argv[2], sys.argv[3]
lines = ["[connection]", "id=wifi-" + ssid, "uuid=" + str(uuid.uuid4()), "type=wifi", "interface-name=wlan0",
         "autoconnect=true", "autoconnect-priority=10", "autoconnect-retries=0", "",
         "[wifi]", "mode=infrastructure", "ssid=" + ssid, ""]
if psk:
    lines += ["[wifi-security]", "key-mgmt=wpa-psk", "psk=" + psk, ""]
lines += ["[ipv4]", "method=auto", "", "[ipv6]", "method=ignore", ""]
open(out, "w").write(chr(10).join(lines))
PYW
fi
VER="$(cat "$F/app/VERSION")"

# enkel ons image exporteren (niet het gewone Lite-image)
touch "$PIGEN/stage2/SKIP_IMAGES"
# ONLY_KHZS=1: basissysteem (stage0-2) hergebruiken, enkel de KHZS-stap opnieuw bouwen (minuten i.p.v. een uur)
if [ "${ONLY_KHZS:-0}" = 1 ]; then
	touch "$PIGEN/stage0/SKIP" "$PIGEN/stage1/SKIP" "$PIGEN/stage2/SKIP"
	export CONTINUE=1
else
	rm -f "$PIGEN/stage0/SKIP" "$PIGEN/stage1/SKIP" "$PIGEN/stage2/SKIP"
fi
rm -f "$PIGEN/stage2/EXPORT_IMAGE.bak"

cat > "$PIGEN/config" <<EOF
IMG_NAME=khzs-timing
PI_GEN_RELEASE="KHZS Timing $VER"
RELEASE=trixie
DEPLOY_COMPRESSION=xz
COMPRESSION_LEVEL=6
TARGET_HOSTNAME=khzs-timing
FIRST_USER_NAME=khzs
FIRST_USER_PASS='$PI_PASSWORD'
DISABLE_FIRST_BOOT_USER_RENAME=1
ENABLE_SSH=1
LOCALE_DEFAULT=nl_BE.UTF-8
KEYBOARD_KEYMAP=be
KEYBOARD_LAYOUT="Belgian"
TIMEZONE_DEFAULT=Europe/Brussels
WPA_COUNTRY=BE
STAGE_LIST="stage0 stage1 stage2 stage-khzs"
EOF

cd "$PIGEN"
echo "== bouw KHZS Timing $VER (pi-gen arm64, trixie) – dit duurt 30 à 90 minuten =="
CONTINUE="${CONTINUE:-0}" PRESERVE_CONTAINER=1 ./build-docker.sh

IMG="$(ls -t "$PIGEN"/deploy/*khzs-timing*.img.xz | head -1)"
mkdir -p "$REPO/dist"
OUT="khzs-timing-$VER$SUFFIX.img.xz"
cp "$IMG" "$REPO/dist/$OUT"
(cd "$REPO/dist" && sha256sum "$OUT" | tee "$OUT.sha256")
echo "== klaar: dist/$OUT =="
