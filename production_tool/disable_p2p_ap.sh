#!/system/bin/sh

# Decides, on every boot, whether the factory Wi-Fi Direct/P2P AP (visible as
# "CubeJ-xxxxxx") stays up. Only ever touches p2p-wlan0-0 and its dedicated
# dnsmasq; the regular home Wi-Fi connection on wlan0 is left alone.
#
# No home network configured yet  -> SETUP MODE: leave the AP up so a phone
#                                    can reach the Wi-Fi setup page, and
#                                    replace the OEM's DHCP-only dnsmasq with
#                                    one that also answers DNS, so phones show
#                                    a captive-portal "sign in" prompt instead
#                                    of quietly falling back to mobile data.
# Home network configured         -> disable the AP, as before.
#
# This AP's WPA2 passphrase is the fixed value "12345678" (verified on a real
# device 2026-08-02). An earlier version of this script claimed the passphrase
# was unobtainable because it derives from an OEM-encrypted property
# (ro.default.p2p.pwd.enc.b64); that was an untested inference, and wrong.
#
# Because that passphrase is a published constant, everything reachable over
# this AP is reachable by anyone within radio range. That is why the AP is shut
# off the moment it is no longer needed, and why config_server.py serves only
# the Wi-Fi form - not status, config, logs or OTA - to clients arriving on it.
#
# The file is still called disable_p2p_ap.sh, and its init service is still
# disable_p2p_ap, purely so OTA can keep updating it: OTA may only overwrite
# files at paths already present on the device (see readme).

LOG=/data/local/cubej1_p2p_ap.log
WPA_CONF=/data/misc/wifi/wpa_supplicant.conf
WPA_CLI="wpa_cli -p /data/misc/wifi/sockets"
SETUP_AP_IP=192.168.100.1
SETUP_AP_IFACE=p2p-wlan0-0
DHCP_RANGE=192.168.100.2,192.168.100.100,255.255.255.0,14400m

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"
}

# ---------------------------------------------------------------------------
# Is a real home network configured?
# ---------------------------------------------------------------------------

# The wpa_supplicant.conf shipped on the USB stick carries a placeholder
# ssid="ssid"; treat that, an empty ssid and a missing file all as "not set up
# yet", so an untouched install lands in setup mode instead of disabling the
# only AP the user could have reached it on.
configured_ssid() {
    sed -n 's/^[[:space:]]*ssid="\(.*\)"[[:space:]]*$/\1/p' "$WPA_CONF" 2>/dev/null | head -n 1
}

wifi_configured() {
    ssid="$(configured_ssid)"
    [ -n "$ssid" ] && [ "$ssid" != "ssid" ]
}

# ---------------------------------------------------------------------------
# P2P AP teardown (configured case)
# ---------------------------------------------------------------------------

ensure_p2p_disabled_config() {
    if [ -f "$WPA_CONF" ] && ! grep -q '^p2p_disabled=1' "$WPA_CONF"; then
        log "adding p2p_disabled=1 to wpa_supplicant.conf"
        # Set through wpa_cli, not by editing the file: this build runs with
        # update_config=1 and rewrites the file on its own, silently dropping
        # hand-made edits.
        $WPA_CLI -i wlan0 set p2p_disabled 1 >/dev/null 2>&1
        $WPA_CLI -i wlan0 save_config >/dev/null 2>&1
    fi
}

ensure_p2p_enabled_config() {
    if [ -f "$WPA_CONF" ] && grep -q '^p2p_disabled=1' "$WPA_CONF"; then
        log "clearing p2p_disabled for setup mode (takes effect next boot)"
        $WPA_CLI -i wlan0 set p2p_disabled 0 >/dev/null 2>&1
        $WPA_CLI -i wlan0 save_config >/dev/null 2>&1
    fi
}

p2p_dnsmasq_pids() {
    ps | grep '[d]nsmasq' | while read user pid rest; do
        cmd="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)"
        case "$cmd" in
            *"--dhcp-range=192.168.100."*|*"$SETUP_AP_IFACE"*)
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
    $WPA_CLI -i $SETUP_AP_IFACE p2p_group_remove $SETUP_AP_IFACE >/dev/null 2>&1
    $WPA_CLI -i wlan0 p2p_group_remove $SETUP_AP_IFACE >/dev/null 2>&1
    $WPA_CLI -i wlan0 p2p_stop_find >/dev/null 2>&1
    $WPA_CLI -i wlan0 p2p_flush >/dev/null 2>&1

    ifconfig $SETUP_AP_IFACE down >/dev/null 2>&1
    stop_p2p_dnsmasq
}

p2p_dnsmasq_is_active() {
    [ -n "$(p2p_dnsmasq_pids)" ]
}

p2p_is_active() {
    ip addr show $SETUP_AP_IFACE >/dev/null 2>&1 && return 0
    p2p_dnsmasq_is_active && return 0
    return 1
}

disable_p2p() {
    log "Home Wi-Fi is configured - stopping factory P2P/AP"
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
}

# ---------------------------------------------------------------------------
# Setup mode (no home network yet)
# ---------------------------------------------------------------------------

wait_for_p2p_iface() {
    i=0
    while [ "$i" -lt 30 ]; do
        if ip addr show $SETUP_AP_IFACE 2>/dev/null | grep -q "$SETUP_AP_IP"; then
            return 0
        fi
        i=$((i + 1))
        sleep 2
    done
    return 1
}

start_setup_dns() {
    # The OEM starts dnsmasq with --dhcp-range only, so it hands out an address
    # and nothing else. A phone's captive-portal probe then resolves nowhere,
    # the network is marked "no internet", and traffic silently goes to mobile
    # data instead - which is exactly what made the setup page unreachable in
    # testing. Replacing it with one that answers every name with our own
    # address is what turns this into a real captive portal.
    for pid in $(p2p_dnsmasq_pids); do
        log "replacing OEM dnsmasq (pid=$pid) for setup mode"
        kill "$pid" >/dev/null 2>&1
    done
    sleep 1

    # Bound to the P2P interface only. The OEM's binds 0.0.0.0, which would
    # also answer on the home network once wlan0 comes up.
    dnsmasq --dhcp-range=$DHCP_RANGE \
            --address=/#/$SETUP_AP_IP \
            --interface=$SETUP_AP_IFACE \
            --except-interface=lo \
            --bind-interfaces >/dev/null 2>&1 &
    sleep 1
    if p2p_dnsmasq_is_active; then
        log "setup-mode dnsmasq running (DHCP + wildcard DNS on $SETUP_AP_IFACE)"
    else
        log "setup-mode dnsmasq failed to start - falling back to OEM DHCP only"
    fi
}

enable_setup_mode() {
    log "No home Wi-Fi configured - entering setup mode, leaving factory AP up"
    ensure_p2p_enabled_config

    if wait_for_p2p_iface; then
        log "setup AP is up on $SETUP_AP_IFACE ($SETUP_AP_IP)"
        start_setup_dns
        exit 0
    fi

    log "setup AP did not appear on $SETUP_AP_IFACE within timeout"
    exit 1
}

# ---------------------------------------------------------------------------

if wifi_configured; then
    disable_p2p
else
    enable_setup_mode
fi
