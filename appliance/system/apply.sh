#!/bin/bash
# KHZS Timing – systeem bijwerken na een update. Draait als root via de systeemdienst, ENKEL na bevestiging in het
# beheer. Idempotent, herstart de Pi nooit.
#   apply.sh --dry-run   toont enkel wat er zou veranderen (één regel per wijziging, prefix "WIJZIGING: ")
#   apply.sh             voert de wijzigingen uit
set -u
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1
SRC="$(cd "$(dirname "$0")/.." && pwd)"        # .../current/appliance
CHANGED_UNITS=0
CHANGED_NM=0
CHANGED_JOURNAL=0

change() { echo "WIJZIGING: $*"; }

sync_file() {   # sync_file bron doel modus omschrijving  → 0 als (te) wijzigen
	local src="$1" dst="$2" mode="$3" what="$4"
	[ -f "$src" ] || return 1
	if [ ! -f "$dst" ] || ! cmp -s "$src" "$dst"; then
		change "$what ($dst)"
		[ "$DRY" = 1 ] || install -D -m "$mode" "$src" "$dst"
		return 0
	fi
	return 1
}

# ---- systemd-diensten
for u in "$SRC"/systemd/*.service; do
	[ -f "$u" ] || continue
	sync_file "$u" "/etc/systemd/system/$(basename "$u")" 644 "dienst $(basename "$u")" && CHANGED_UNITS=1
done

# ---- configuratie
sync_file "$SRC/conf/watchdog.conf" /etc/systemd/system.conf.d/khzs-watchdog.conf 644 "watchdog" || true
sync_file "$SRC/conf/journald.conf" /etc/systemd/journald.conf.d/khzs.conf 644 "logboek (journald)" && CHANGED_JOURNAL=1
sync_file "$SRC/conf/nm-khzs.conf" /etc/NetworkManager/conf.d/khzs.conf 644 "NetworkManager" && CHANGED_NM=1
sync_file "$SRC/conf/motd" /etc/motd 644 "welkomsttekst SSH" || true
sync_file "$SRC/bin/khzs" /usr/local/bin/khzs 755 "commando khzs" || true
for f in launch.py wait_server.py khzs_agent.py kiosk_start.py; do
	if python3 -m py_compile "$SRC/agent/$f" 2>/dev/null; then
		sync_file "$SRC/agent/$f" "/opt/khzs/agent/$f" 755 "reservekopie systeemdienst: $f" || true
	fi
done

# ---- pakketten (ontbrekende worden geïnstalleerd, niets wordt verwijderd)
MISSING=""
if [ -f "$SRC/system/packages.txt" ]; then
	for p in $(grep -v '^#' "$SRC/system/packages.txt"); do
		dpkg -s "$p" >/dev/null 2>&1 || MISSING="$MISSING $p"
	done
fi
if [ -n "$MISSING" ]; then
	change "pakketten installeren:$MISSING"
	if [ "$DRY" = 0 ]; then
		if timeout 5 bash -c '</dev/tcp/deb.debian.org/80' 2>/dev/null; then
			DEBIAN_FRONTEND=noninteractive apt-get -qq -o Acquire::ForceIPv4=true update && \
			DEBIAN_FRONTEND=noninteractive apt-get -qq -y --no-install-recommends install $MISSING \
				|| echo "FOUT: pakketten installeren mislukt"
		else
			echo "FOUT: geen internet voor pakketten:$MISSING"
		fi
	fi
fi

# ---- opstartscherm (thema, opstartregel, initramfs); zichtbaar vanaf de volgende start
if [ -f "$SRC/system/bootscreen.sh" ]; then
	if [ "$DRY" = 1 ]; then bash "$SRC/system/bootscreen.sh" --dry-run; else bash "$SRC/system/bootscreen.sh"; fi
fi

# ---- migraties per versie (eenmalig): appliance/system/migrations/NNN-naam.sh
for m in "$SRC"/system/migrations/*.sh; do
	[ -f "$m" ] || continue
	id="$(basename "$m")"
	[ -f "/var/lib/khzs/migrations/$id" ] && continue
	change "migratie $id: $(sed -n '2s/^# *//p' "$m")"
	if [ "$DRY" = 0 ]; then
		mkdir -p /var/lib/khzs/migrations
		if bash "$m"; then touch "/var/lib/khzs/migrations/$id"; else echo "FOUT: migratie $id mislukt"; fi
	fi
done

[ "$DRY" = 1 ] && exit 0

# ---- toepassen
if [ "$CHANGED_UNITS" = 1 ]; then
	systemctl daemon-reload
	systemctl enable khzs-timing.service khzs-agent.service 2>/dev/null || true
	systemctl try-restart khzs-kiosk.service || true
fi
[ "$CHANGED_JOURNAL" = 1 ] && systemctl restart systemd-journald
[ "$CHANGED_NM" = 1 ] && { nmcli general reload conf 2>/dev/null || systemctl reload NetworkManager || true; }
echo "systeem bijgewerkt"
