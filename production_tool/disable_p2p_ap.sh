#!/system/bin/sh

# Stops the factory-default Wi-Fi Direct/P2P AP (visible as "CubeJ-xxxxxx").
# Only targets p2p-wlan0-0 and its dedicated dnsmasq; the regular home
# Wi-Fi connection on wlan0 is left untouched.
#
# This AP's WPA2 passphrase is derived from an OEM-encrypted Android
# property (ro.default.p2p.pwd.enc.b64) we have no key for, so it can't
# be joined by anything we control and serves no purpose for this
# bridge - disable it rather than leave it broadcasting with no usable
# credentials.

LOG=/data/local/cubej1_p2p_ap.log
WPA_CONF=/data/misc/wifi/wpa_supplicant.conf
WPA_CLI="wpa_cli -p /data/misc/wifi/sockets"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"
}

ensure_p2p_disabled_config() {
    if [ -f "$WPA_CONF" ] && ! grep -q '^p2p_disabled=1' "$WPA_CONF"; then
        log "adding p2p_disabled=1 to wpa_supplicant.conf"
        echo "p2p_disabled=1" >> "$WPA_CONF"
        chmod 660 "$WPA_CONF"
        chown system:wifi "$WPA_CONF"
        $WPA_CLI -i wlan0 reconfigure >/dev/null 2>&1
    fi
}

p2p_dnsmasq_pids() {
    ps | grep '[d]nsmasq' | while read user pid rest; do
        cmd="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)"
        case "$cmd" in
            *"--dhcp-range=192.168.100."*|*"p2p-wlan0-0"*)
                echo "$pid"
                ;;
        esac
    done
}

stop_p2p_dnsmasq() {
    for pid in $(p2p_dnsmasq_pids); do
        log "stopping P2P AP dnsmasq: pid=$pid"
        kill "$pid" >/dev/null 2>&1
    done
}

stop_p2p_once() {
    $WPA_CLI -i p2p-wlan0-0 p2p_group_remove p2p-wlan0-0 >/dev/null 2>&1
    $WPA_CLI -i wlan0 p2p_group_remove p2p-wlan0-0 >/dev/null 2>&1
    $WPA_CLI -i wlan0 p2p_stop_find >/dev/null 2>&1
    $WPA_CLI -i wlan0 p2p_flush >/dev/null 2>&1

    ifconfig p2p-wlan0-0 down >/dev/null 2>&1
    stop_p2p_dnsmasq
}

p2p_dnsmasq_is_active() {
    [ -n "$(p2p_dnsmasq_pids)" ]
}

p2p_is_active() {
    ip addr show p2p-wlan0-0 >/dev/null 2>&1 && return 0
    p2p_dnsmasq_is_active && return 0
    return 1
}

log "Stopping factory P2P/AP"
ensure_p2p_disabled_config

i=0
while [ "$i" -lt 30 ]; do
    stop_p2p_once
    if ! p2p_is_active; then
        log "P2P/AP stopped"
        exit 0
    fi
    i=$((i + 1))
    sleep 2
done

log "Timed out stopping P2P/AP"
exit 1
