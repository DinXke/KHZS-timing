#!/bin/bash -e
# KHZS Timing appliance: bestanden plaatsen en diensten instellen (draait op de bouwhost; on_chroot = in het image)

F="${STAGE_DIR}/00-khzs/files"
VER="$(cat "${F}/app/VERSION")"

# ---- applicatie: /opt/khzs/releases/<versie>, /opt/khzs/current -> die versie, systeemdienst in /opt/khzs/agent
install -d -m 755 "${ROOTFS_DIR}/opt/khzs/releases" "${ROOTFS_DIR}/opt/khzs/agent"
rm -rf "${ROOTFS_DIR}/opt/khzs/releases/${VER}-image"      # bij hergebruik (ONLY_KHZS) niet in de oude map kopiëren
cp -a "${F}/app" "${ROOTFS_DIR}/opt/khzs/releases/${VER}-image"
ln -sfn "/opt/khzs/releases/${VER}-image" "${ROOTFS_DIR}/opt/khzs/current"
install -m 755 "${F}/agent/"*.py "${ROOTFS_DIR}/opt/khzs/agent/"
install -m 755 "${F}/app/appliance/bin/khzs" "${ROOTFS_DIR}/usr/local/bin/khzs"

# ---- systemd-diensten en instellingen
install -m 644 "${F}/systemd/"*.service "${ROOTFS_DIR}/etc/systemd/system/"
install -d "${ROOTFS_DIR}/etc/systemd/system.conf.d" "${ROOTFS_DIR}/etc/systemd/journald.conf.d" \
	"${ROOTFS_DIR}/etc/NetworkManager/conf.d" "${ROOTFS_DIR}/etc/NetworkManager/dnsmasq-shared.d"
install -m 644 "${F}/conf/watchdog.conf" "${ROOTFS_DIR}/etc/systemd/system.conf.d/khzs-watchdog.conf"
install -m 644 "${F}/conf/journald.conf" "${ROOTFS_DIR}/etc/systemd/journald.conf.d/khzs.conf"
install -m 644 "${F}/conf/nm-khzs.conf" "${ROOTFS_DIR}/etc/NetworkManager/conf.d/khzs.conf"
install -m 644 "${F}/conf/motd" "${ROOTFS_DIR}/etc/motd"
# standaard Wi-Fi (gemaakt door build-image.sh uit secrets.env)
if [ -f "${F}/wifi-home.nmconnection" ]; then
	install -d -m 700 "${ROOTFS_DIR}/etc/NetworkManager/system-connections"
	install -m 600 "${F}/wifi-home.nmconnection" "${ROOTFS_DIR}/etc/NetworkManager/system-connections/wifi-home.nmconnection"
else
	# publiek image: nooit een Wi-Fi uit een vorige (privé)build meenemen (ONLY_KHZS hergebruikt de rootfs)
	rm -f "${ROOTFS_DIR}/etc/NetworkManager/system-connections/wifi-home.nmconnection"
fi

# ---- gegevensmap met standaardinstellingen (beheerderscode, hotspot)
install -d -m 755 "${ROOTFS_DIR}/var/lib/khzs"
install -m 600 "${F}/conf/settings.json" "${ROOTFS_DIR}/var/lib/khzs/settings.json"
install -m 600 "${F}/conf/system.json" "${ROOTFS_DIR}/var/lib/khzs/system.json"

# ---- HDMI: altijd beeld (ook als de TV later aangesloten wordt), geen overscan, bluetooth uit, seriële console aan
CFG="${ROOTFS_DIR}/boot/firmware/config.txt"
if ! grep -q "KHZS" "${CFG}"; then
	cat >> "${CFG}" <<'EOT'

# --- KHZS Timing ---
[all]
hdmi_force_hotplug=1
disable_overscan=1
dtoverlay=disable-bt
enable_uart=1
EOT
fi
CMD="${ROOTFS_DIR}/boot/firmware/cmdline.txt"
grep -q "consoleblank=0" "${CMD}" || sed -i '1 s/$/ consoleblank=0 quiet loglevel=3 logo.nologo vt.global_cursor_default=0/' "${CMD}"

on_chroot << EOF
set -e
# noodwachtwoord altijd opnieuw zetten (ONLY_KHZS hergebruikt de gebruikers van de vorige build)
echo "${FIRST_USER_NAME}:${FIRST_USER_PASS}" | chpasswd
# gebruikers: 'khzs' (server, bestaat al als eerste gebruiker), 'kiosk' (HDMI-scherm)
id kiosk >/dev/null 2>&1 || useradd -m -s /usr/sbin/nologin -G video,render,input kiosk
chown -R khzs:khzs /var/lib/khzs
chown root:root /var/lib/khzs/system.json
chmod 755 /var/lib/khzs

systemctl enable NetworkManager khzs-agent.service khzs-timing.service khzs-kiosk.service avahi-daemon
systemctl disable getty@tty1.service || true
# wat een wedstrijdserver niet nodig heeft (minder schrijfwerk op de SD-kaart, snellere start)
for s in bluetooth hciuart triggerhappy ModemManager apt-daily.timer apt-daily-upgrade.timer man-db.timer \
         e2scrub_all.timer fstrim.timer cups cups-browsed rpi-eeprom-update; do
	systemctl disable "\$s" 2>/dev/null || true
	systemctl mask "\$s" 2>/dev/null || true
done
systemctl set-default multi-user.target
# opstartscherm (Plymouth-thema, opstartregel, initramfs) – zelfde script als na een update
bash /opt/khzs/current/appliance/system/bootscreen.sh
EOF
