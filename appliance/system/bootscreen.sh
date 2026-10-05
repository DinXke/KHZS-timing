#!/bin/bash
# HZS Timing – opstartscherm (Plymouth) installeren of bijwerken. Idempotent; als root.
#   bootscreen.sh --dry-run   toont enkel wat er zou veranderen ("WIJZIGING: ...")
#   bootscreen.sh             voert uit (zichtbaar vanaf de volgende (her)start)
# Gebruikt door: het bouwen van het image (01-run.sh), apply.sh na een update en "khzs splash install".
set -u
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1
SRC="$(cd "$(dirname "$0")/.." && pwd)/plymouth"          # .../appliance/plymouth
THEME=/usr/share/plymouth/themes/khzs
BOOT=/boot/firmware
[ -d "$BOOT" ] || BOOT=/boot
INITRD=0

change() { echo "WIJZIGING: $*"; }

[ -f "$SRC/khzs/khzs.script" ] || { echo "geen opstartscherm in $SRC"; exit 0; }
if ! command -v plymouth >/dev/null; then
	[ "$DRY" = 1 ] && change "opstartscherm installeren (na de pakketten plymouth en plymouth-label)"
	[ "$DRY" = 1 ] || echo "FOUT: plymouth is niet geïnstalleerd"
	exit 0
fi

# ---- thema-bestanden
if ! diff -rq "$SRC/khzs" "$THEME" >/dev/null 2>&1; then
	change "opstartscherm: thema HZS Timing"
	if [ "$DRY" = 0 ]; then
		rm -rf "$THEME.new" && cp -r "$SRC/khzs" "$THEME.new" && chmod -R a+rX "$THEME.new" \
			&& rm -rf "$THEME" && mv "$THEME.new" "$THEME"
	fi
	INITRD=1
fi

# ---- grijswaarden-antialiasing (geen gekleurde randjes in de tekst)
if ! cmp -s "$SRC/khzs-fonts.conf" /etc/fonts/conf.d/99-khzs.conf; then
	change "opstartscherm: lettertype-instelling"
	[ "$DRY" = 0 ] && install -D -m 644 "$SRC/khzs-fonts.conf" /etc/fonts/conf.d/99-khzs.conf
	INITRD=1
fi

# ---- standaardthema
if [ "$(plymouth-set-default-theme 2>/dev/null)" != khzs ]; then
	change "opstartscherm: standaardthema khzs"
	[ "$DRY" = 0 ] && plymouth-set-default-theme khzs
	INITRD=1
fi

# ---- opstartscherm blijft staan tot de server antwoordt; daarna neemt het HDMI-scherm over
DROP=/etc/systemd/system/plymouth-quit.service.d/khzs.conf
if ! cmp -s "$SRC/plymouth-quit-khzs.conf" "$DROP"; then
	change "opstartscherm: blijft staan tot de server klaar is"
	if [ "$DRY" = 0 ]; then
		install -D -m 644 "$SRC/plymouth-quit-khzs.conf" "$DROP"
		systemctl daemon-reload 2>/dev/null || true
	fi
fi

# ---- opstartregel (cmdline.txt): grafisch opstartscherm, ook met de seriële console aan
CMD="$BOOT/cmdline.txt"
if [ -f "$CMD" ]; then
	for a in splash plymouth.ignore-serial-consoles quiet loglevel=3 logo.nologo vt.global_cursor_default=0; do
		if ! tr ' ' '\n' < "$CMD" | grep -qx -- "$a"; then
			change "opstartregel: $a"
			[ "$DRY" = 0 ] && sed -i "1 s/\$/ $a/" "$CMD"
		fi
	done
fi

# ---- config.txt: geen regenboogscherm van de firmware
CFG="$BOOT/config.txt"
if [ -f "$CFG" ] && ! grep -q '^disable_splash=1' "$CFG"; then
	change "config.txt: disable_splash=1"
	[ "$DRY" = 0 ] && printf '\n# --- KHZS Timing: opstartscherm ---\n[all]\ndisable_splash=1\n' >> "$CFG"
fi

# ---- initramfs: het thema zit er ook in, zodat het scherm al na enkele seconden verschijnt
if [ "$INITRD" = 1 ] && command -v update-initramfs >/dev/null; then
	change "initramfs bijwerken (1-2 minuten)"
	if [ "$DRY" = 0 ]; then
		update-initramfs -u -k all >/dev/null 2>&1 \
			|| echo "FOUT: update-initramfs mislukt – het opstartscherm verschijnt dan pas iets later"
	fi
fi
[ "$DRY" = 0 ] && echo "opstartscherm bijgewerkt (zichtbaar bij de volgende start)"
exit 0
