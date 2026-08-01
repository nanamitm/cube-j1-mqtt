#!/usr/bin/python
# -*- coding: utf-8 -*-
"""
mqtt_bridge.py  -  Wi-SUN B-route -> ECHONET Lite -> Home Assistant MQTT
Python 2.7 stdlib only: termios, fcntl, select, socket, struct, json, os
"""

from __future__ import print_function

import os
import sys
import json
import time
import struct
import socket
import select
import binascii
import termios
import fcntl
import collections
import re
import threading
import datetime

CONFIG_PATH = "/data/local/config.json"
LOG_PATH    = "/data/local/mqtt_bridge.log"
SERIAL_LOG_PATH = "/data/local/serial.log"
STATUS_PATH = "/data/local/mqtt_status.json"
OTA_STATUS_PATH = "/data/local/ota_status.json"
PAN_CACHE_PATH = "/data/local/pan_cache.json"

# Skip the PAN scan on reconnect by re-using the last PAN that worked
# (config.json's pan_cache_enabled overrides this).
PAN_CACHE_ENABLED = True

# Log files rotate to ".1" once they reach this size (config.json's
# log_max_bytes overrides this); the previous ".1" is dropped, so each log's
# on-disk footprint is bounded to roughly 2x this value.
LOG_MAX_BYTES = 10 * 1024 * 1024

LED_R = "/sys/class/leds/red/brightness"
LED_G = "/sys/class/leds/green/brightness"
LED_B = "/sys/class/leds/blue/brightness"

def led_rgb(r, g, b):
    for path, val in ((LED_R, r), (LED_G, g), (LED_B, b)):
        try:
            with open(path, 'w') as f:
                f.write(str(val) + '\n')
        except Exception:
            pass

def led_read():
    result = []
    for path in (LED_R, LED_G, LED_B):
        try:
            with open(path) as f:
                result.append(int(f.read().strip()))
        except Exception:
            result.append(0)
    return tuple(result)

_log_file = None
_serial_log_file = None
_status = {}

def _rotate_if_needed(file_obj, path):
    """Roll path over to path+'.1' once it reaches LOG_MAX_BYTES.

    Returns the file object to keep using (a freshly opened one if
    rotation happened, or the original/None on failure).
    """
    if not file_obj:
        return file_obj
    try:
        if os.fstat(file_obj.fileno()).st_size < LOG_MAX_BYTES:
            return file_obj
        file_obj.close()
        backup_path = path + ".1"
        if os.path.exists(backup_path):
            os.remove(backup_path)
        os.rename(path, backup_path)
        return open(path, "a")
    except Exception:
        try:
            return open(path, "a")
        except Exception:
            return None

def log(msg):
    ts = now_str()
    line = "[{}] {}\n".format(ts, msg)
    global _log_file
    if _log_file:
        _log_file = _rotate_if_needed(_log_file, LOG_PATH)
    if _log_file:
        try:
            _log_file.write(line)
            _log_file.flush()
        except Exception:
            pass
    else:
        sys.stderr.write(line)
        sys.stderr.flush()

def serial_log(direction, data):
    global _serial_log_file
    if not _serial_log_file:
        return
    _serial_log_file = _rotate_if_needed(_serial_log_file, SERIAL_LOG_PATH)
    if not _serial_log_file:
        return
    if isinstance(data, bytes):
        data = data.decode("ascii", "replace")
    ts = now_str()
    try:
        _serial_log_file.write("[{}] {} {}\n".format(ts, direction, data.rstrip("\r\n")))
        _serial_log_file.flush()
    except Exception:
        pass

def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)

def load_ota_status():
    try:
        with open(OTA_STATUS_PATH) as f:
            return json.load(f)
    except Exception:
        return {"state": "unknown", "message": "OTA status is not available"}

def validate_config(cfg):
    missing = []
    br_id = cfg.get("br_id", "")
    br_pwd = cfg.get("br_pwd", "")
    mqtt_host = cfg.get("mqtt_host", "")

    if not br_id or br_id == "00000000000000000000000000000000":
        missing.append("br_id")
    if not br_pwd or br_pwd == "0123456789AB":
        missing.append("br_pwd")
    if not mqtt_host or mqtt_host == "192.168.1.254":
        missing.append("mqtt_host")
    return missing

def wait_for_config():
    while True:
        try:
            cfg = load_config()
            missing = validate_config(cfg)
            if not missing:
                return cfg
            write_status(bridge_started_at=_status.get("bridge_started_at") or now_str(),
                         configuration_required=True,
                         missing_config=missing,
                         mqtt_connected=False,
                         wisun_connected=False,
                         last_error="Configuration required: {}".format(", ".join(missing)))
            log("Configuration required: {} - waiting for Web UI save".format(", ".join(missing)))
        except Exception as e:
            write_status(bridge_started_at=_status.get("bridge_started_at") or now_str(),
                         configuration_required=True,
                         mqtt_connected=False,
                         wisun_connected=False,
                         last_error="Config load failed: {}".format(e))
            log("Config load failed: {} - retry in 10s".format(e))
        time.sleep(10)

JST_OFFSET_SECONDS = 9 * 3600

def now_str():
    # The device clock runs in UTC with no timezone configured, so apply a
    # fixed JST (UTC+9) offset here rather than relying on system tzdata.
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() + JST_OFFSET_SECONDS))

def write_status(**kwargs):
    global _status
    _status.update(kwargs)
    _status["updated_at"] = now_str()
    tmp_path = STATUS_PATH + ".tmp"
    try:
        with open(tmp_path, "w") as f:
            json.dump(_status, f, indent=2, sort_keys=True)
            f.write("\n")
        os.rename(tmp_path, STATUS_PATH)
    except Exception as e:
        log("status write failed: {}".format(e))
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

def epcs_to_hex(epcs):
    return ["0x{:02X}".format(epc) for epc in epcs]

def measurement_summary(m):
    keys = ("power_w", "energy_forward_kwh", "energy_reverse_kwh",
            "current_r_a", "current_t_a",
            "one_minute_energy_forward_kwh", "one_minute_energy_reverse_kwh",
            "fixed_time_energy_forward_kwh", "fixed_time_energy_reverse_kwh",
            "operation_status", "fault_status", "meter_date", "meter_time",
            "installation_place", "maker_code", "serial_number")
    result = {}
    for key in keys:
        if key in m:
            result[key] = m[key]
    return result

# ---------------------------------------------------------------------------
# Serial port (termios, no pyserial)
# ---------------------------------------------------------------------------

def open_serial(port, baud=115200):
    fd = os.open(port, os.O_RDWR | os.O_NOCTTY)

    attrs = list(termios.tcgetattr(fd))
    iflag, oflag, cflag, lflag = attrs[0], attrs[1], attrs[2], attrs[3]

    # raw input
    iflag &= ~(termios.IGNBRK | termios.BRKINT | termios.PARMRK |
               termios.ISTRIP | termios.INLCR  | termios.IGNCR  |
               termios.ICRNL  | termios.IXON)
    oflag &= ~termios.OPOST
    cflag &= ~(termios.CSIZE | termios.PARENB)
    cflag |=  termios.CS8 | termios.CREAD | termios.CLOCAL
    lflag &= ~(termios.ECHO | termios.ECHONL | termios.ICANON |
               termios.ISIG | termios.IEXTEN)

    baud_map = {
        9600:   termios.B9600,
        19200:  termios.B19200,
        38400:  termios.B38400,
        57600:  termios.B57600,
        115200: termios.B115200,
    }
    baud_const = baud_map.get(baud, termios.B115200)

    cc = attrs[6]
    # attrs[6] must be returned in the same type tcgetattr gave us.
    # On this device Python 2.7 it is a list of 32 ints; tcsetattr rejects bytes.
    if isinstance(cc, list):
        cc_list = list(cc)
        cc_list[termios.VMIN]  = 1
        cc_list[termios.VTIME] = 0
        attrs[6] = cc_list
    else:
        # bytes/bytearray path
        cc_arr = bytearray(cc)
        cc_arr[termios.VMIN]  = 1
        cc_arr[termios.VTIME] = 0
        attrs[6] = bytes(cc_arr)

    attrs[0], attrs[1], attrs[2], attrs[3] = iflag, oflag, cflag, lflag
    attrs[4] = baud_const
    attrs[5] = baud_const

    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    termios.tcflush(fd, termios.TCIOFLUSH)
    return fd

def serial_write(fd, data):
    serial_log("TX", data)
    if isinstance(data, bytes):
        os.write(fd, data)
    else:
        os.write(fd, data.encode("ascii"))

def serial_readline(fd, timeout=10):
    """Read one CRLF-terminated line; return decoded str or None on timeout."""
    buf = b""
    deadline = time.time() + timeout
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        r, _, _ = select.select([fd], [], [], min(remaining, 0.5))
        if not r:
            continue
        ch = os.read(fd, 1)
        if not ch:
            continue
        buf += ch
        if buf.endswith(b"\r\n"):
            line = buf[:-2].decode("ascii", errors="replace")
            serial_log("RX", line)
            return line
    if buf:
        line = buf.decode("ascii", errors="replace")
        serial_log("RX", line)
        return line
    return None

def _led_blink(stop_event, colors, interval=0.2):
    i = 0
    while not stop_event.is_set():
        led_rgb(*colors[i % len(colors)])
        i += 1
        stop_event.wait(interval)

def skcommand(fd, cmd, timeout=10):
    """Send one SKSTACK command; return list of response lines (up to OK/FAIL)."""
    orig_led = led_read()
    stop_event = threading.Event()
    t = threading.Thread(target=_led_blink,
                         args=(stop_event, [(0, 255, 0), (0, 0, 255)]))
    t.daemon = True
    t.start()

    serial_write(fd, cmd + "\r\n")
    lines = []
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            line = serial_readline(fd, timeout=max(0.5, deadline - time.time()))
            if line is None:
                break
            lines.append(line)
            if line in ("OK", ) or line.startswith("FAIL"):
                break
    finally:
        stop_event.set()
        t.join(timeout=1)
        led_rgb(*orig_led)
    return lines

# ---------------------------------------------------------------------------
# Scan settings
# ---------------------------------------------------------------------------

SCAN_DURATION_BASE = 4
SCAN_RETRY_LIMIT = 10

MAX_CONSECUTIVE_TIMEOUTS = 3

PROPERTY_MAP_MAX_RETRIES = 3
PROPERTY_MAP_RETRY_DELAY = 3

# ---------------------------------------------------------------------------
# SKSTACK-IP / Wi-SUN B-route connection
# ---------------------------------------------------------------------------

def skscan(fd):
    """Active scan with retries; returns best PAN info dict or empty dict."""
    duration = SCAN_DURATION_BASE
    
    while duration <= SCAN_RETRY_LIMIT:
        # Clear stale lines from previous command/scan cycle.
        termios.tcflush(fd, termios.TCIFLUSH)

        log("SKSCAN try duration={}".format(duration))
        # BP35C0 style scan command: <mode> <channel_mask> <duration> <side>
        serial_write(fd, "SKSCAN 2 FFFFFFFF {} 0\r\n".format(duration))

        pan_list  = []
        current   = {}
        scan_done = False
        deadline  = time.time() + duration
        while time.time() < deadline:
            line = serial_readline(fd, timeout=2)
            if line is None:
                continue
            if line.startswith("EVENT 20"):
                if current:
                    pan_list.append(current)
                current = {}
            elif line.startswith("EVENT 22"):
                if current:
                    pan_list.append(current)
                scan_done = True
                break  # Exit loop once EVENT 22 received
            elif ":" in line and not line.startswith("EVENT"):
                key, _, val = line.strip().partition(":")
                current[key.strip()] = val.strip()

        if pan_list:
            log("SKSCAN found {} PAN(s), selecting best LQI".format(len(pan_list)))
            pan_list.sort(key=lambda p: int(p.get("LQI", "0"), 16), reverse=True)
            return pan_list[0]

        log("SKSCAN no PAN found, retrying with longer duration")
        duration += 1

    return {}

def skll64(fd, mac):
    """Convert MAC address to IPv6 link-local address.

    Reads lines until an IPv6-like substring (hex digits + colons) is found
    and validated. Returns the candidate string or None on timeout.
    """
    serial_write(fd, "SKLL64 {}\r\n".format(mac))
    deadline = time.time() + 10
    while time.time() < deadline:
        line = serial_readline(fd, timeout=2)
        if not line:
            continue
        # skip echoes and obvious non-data lines
        if line.startswith("SKLL64") or line.strip() == "":
            continue
        # extract only hex+colon runs (length threshold to avoid short noise)
        m = re.search(r'([0-9A-Fa-f:]{15,})', line)
        if not m:
            continue
        candidate = m.group(1)
        # validate with inet_pton if available
        try:
            socket.inet_pton(socket.AF_INET6, candidate)
            return candidate
        except Exception:
            # not valid IPv6; continue waiting for a proper response
            log("skll64: received candidate but validation failed: {}".format(candidate))
            continue
    return None

PAN_CACHE_KEYS = ("Channel", "Pan ID", "Addr")

def load_pan_cache():
    """Last PAN that produced a successful join, or None."""
    if not PAN_CACHE_ENABLED:
        return None
    try:
        with open(PAN_CACHE_PATH) as f:
            pan = json.load(f)
    except Exception:
        return None
    if not isinstance(pan, dict) or not all(pan.get(key) for key in PAN_CACHE_KEYS):
        log("PAN cache is incomplete - ignoring it")
        return None
    return pan

def save_pan_cache(pan):
    if not PAN_CACHE_ENABLED:
        return
    data = dict((key, pan.get(key)) for key in PAN_CACHE_KEYS)
    data["LQI"] = pan.get("LQI", "")
    data["saved_at"] = now_str()
    tmp_path = PAN_CACHE_PATH + ".tmp"
    try:
        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        os.rename(tmp_path, PAN_CACHE_PATH)
    except Exception as e:
        log("PAN cache write failed: {}".format(e))
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

def clear_pan_cache(reason):
    if not os.path.exists(PAN_CACHE_PATH):
        return
    try:
        os.remove(PAN_CACHE_PATH)
        log("PAN cache discarded: {}".format(reason))
    except Exception as e:
        log("PAN cache remove failed: {}".format(e))

def join_pan(fd, pan):
    """SKLL64 + channel/PAN registers + SKJOIN. Returns the meter's IPv6."""
    channel = pan["Channel"]
    pan_id  = pan["Pan ID"]
    mac     = pan["Addr"]
    write_status(wisun_channel=channel, wisun_pan_id=pan_id, wisun_lqi=pan.get("LQI", ""))

    ipv6 = skll64(fd, mac)
    if not ipv6:
        raise RuntimeError("SKLL64 failed")
    log("Meter IPv6: {}".format(ipv6))

    skcommand(fd, "SKSREG S2 {}".format(channel))
    skcommand(fd, "SKSREG S3 {}".format(pan_id))

    log("SKJOIN {}".format(ipv6))
    serial_write(fd, "SKJOIN {}\r\n".format(ipv6))

    orig_led = led_read()
    stop_event = threading.Event()
    t = threading.Thread(target=_led_blink,
                         args=(stop_event, [(0, 255, 0), (0, 0, 255)]))
    t.daemon = True
    t.start()
    try:
        deadline = time.time() + 90
        while time.time() < deadline:
            line = serial_readline(fd, timeout=2)
            if line is None:
                continue
            if "EVENT 25" in line:
                log("SKJOIN: connected")
                return ipv6
            if "EVENT 24" in line:
                raise RuntimeError("SKJOIN: PANA authentication failed (EVENT 24)")
    finally:
        stop_event.set()
        t.join(timeout=1)
        led_rgb(*orig_led)

    raise RuntimeError("SKJOIN: timeout")

def wisun_connect(fd, br_id, br_pwd):
    """Full SKSTACK-IP join sequence. Returns IPv6 address of meter."""
    log("SKRESET")
    skcommand(fd, "SKRESET", timeout=5)
    time.sleep(1)

    log("SKSETPWD")
    skcommand(fd, "SKSETPWD C {}".format(br_pwd))

    log("SKSETRBID")
    skcommand(fd, "SKSETRBID {}".format(br_id))

    # Force ASCII-hex ERXUDP payload format so parser stays stable.
    skcommand(fd, "WOPT 1")

    # A scan costs 30-60s (more when it retries) and the meter isn't going
    # to move, so the last PAN that worked is tried first. Anything that
    # goes wrong with it just falls through to a normal scan.
    cached = load_pan_cache()
    if cached:
        log("Using cached PAN: ch={} panId={} mac={} lqi={} (saved {})".format(
            cached.get("Channel"), cached.get("Pan ID"), cached.get("Addr"),
            cached.get("LQI", ""), cached.get("saved_at", "?")))
        write_status(wisun_pan_source="cache")
        try:
            return join_pan(fd, cached)
        except Exception as e:
            log("Join with cached PAN failed: {} - falling back to a full scan".format(e))
            clear_pan_cache("join failed")

    log("SKSCAN (may take up to 60s)")
    write_status(wisun_pan_source="scan")
    pan = skscan(fd)
    if not pan.get("Channel") or not pan.get("Pan ID") or not pan.get("Addr"):
        raise RuntimeError("SKSCAN: no PAN found ({})".format(pan))

    log("PAN found: ch={} panId={} mac={} lqi={}".format(
        pan["Channel"], pan["Pan ID"], pan["Addr"], pan.get("LQI", "")))

    ipv6 = join_pan(fd, pan)
    save_pan_cache(pan)
    return ipv6

# ---------------------------------------------------------------------------
# ECHONET Lite frame builder / parser
# ---------------------------------------------------------------------------

DEFAULT_EPCS = [0xD3, 0xE1, 0xE7, 0xE0, 0xE3, 0xE8]
EXTRA_EPCS = [0x80, 0x81, 0x82, 0x88, 0x8A, 0x8D, 0x97, 0x98, 0xD0, 0xD7, 0xEA, 0xEB]
PROPERTY_MAP_EPC = 0x9F
MISSING_CUMULATIVE_ENERGY = 0xFFFFFFFE

def build_el_get(tid, epcs):
    frame = bytearray()
    frame += b"\x10\x81"                     # EHD1, EHD2
    frame += struct.pack(">H", tid & 0xFFFF) # TID
    frame += b"\x05\xFF\x01"                 # SEOJ: controller
    frame += b"\x02\x88\x01"                 # DEOJ: smart meter
    frame += b"\x62"                         # ESV: Get
    frame += struct.pack("B", len(epcs))     # OPC
    for epc in epcs:
        frame += struct.pack("BB", epc, 0)   # EPC, PDC=0
    return bytes(frame)

def parse_el_response(data):
    """Returns dict {epc_int: bytearray}."""
    if len(data) < 12:
        return {}
    esv = data[10] if isinstance(data[10], int) else ord(data[10])
    opc = data[11] if isinstance(data[11], int) else ord(data[11])
    # Accept Get_Res (0x72) or Get_SNA (0x52)
    if esv not in (0x72, 0x52):
        return {}
    result = {}
    pos = 12
    for _ in range(opc):
        if pos + 2 > len(data):
            break
        epc = data[pos] if isinstance(data[pos], int) else ord(data[pos])
        pdc = data[pos+1] if isinstance(data[pos+1], int) else ord(data[pos+1])
        pos += 2
        if pos + pdc > len(data):
            break
        result[epc] = bytearray(data[pos:pos+pdc])
        pos += pdc
    return result

def parse_property_map(edt):
    if not edt:
        return set()

    count = edt[0]
    prop_map = edt[1:]
    result = set()
    if count < 16:
        for epc in prop_map:
            result.add(epc)
    else:
        for i, b in enumerate(prop_map):
            for bit in range(8):
                if b & (1 << bit):
                    result.add(((bit + 0x08) << 4) + i)
    return result

def format_epcs(epcs):
    return ",".join(["0x{:02X}".format(epc) for epc in sorted(epcs)])

def decode_datetime7(edt):
    if len(edt) < 7:
        return None
    try:
        year = struct.unpack(">H", bytes(edt[0:2]))[0]
        return datetime.datetime(year, edt[2], edt[3], edt[4], edt[5], edt[6])
    except Exception:
        return None

def format_datetime(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None

INSTALLATION_ROOMS = {
    0b00001000: "living_room", 0b00010000: "dining_room", 0b00011000: "kitchen",
    0b00100000: "bathroom",    0b00101000: "toilet",       0b00110000: "washroom",
    0b00111000: "hallway",     0b01000000: "room",         0b01001000: "stairs",
    0b01010000: "entrance",    0b01011000: "closet",       0b01100000: "garden",
    0b01101000: "garage",      0b01110000: "veranda",      0b01111000: "other",
}

def decode_installation_place(val):
    if val == 0x00:
        return "not_set"
    if val == 0xFF:
        return "undefined"
    if val == 0x01:
        return "location_defined"
    if 0x02 <= val <= 0x07:
        return "reserved"
    if 0x80 <= val <= 0xFE:
        return "free_definition({:02X})".format(val)
    room = INSTALLATION_ROOMS.get(val & 0b11111000, "unknown")
    return "{}({})".format(room, val & 0b00000111)

def decode_measurements(props):
    result = {}

    # 80: operation status (0x30=on, 0x31=off)
    if 0x80 in props and len(props[0x80]) >= 1:
        status = props[0x80][0]
        if status == 0x30:
            result["operation_status"] = "on"
        elif status == 0x31:
            result["operation_status"] = "off"
        else:
            result["operation_status"] = "unknown"

    # 81: installation place
    if 0x81 in props and len(props[0x81]) >= 1:
        result["installation_place"] = decode_installation_place(props[0x81][0])

    # 82: standard version information
    if 0x82 in props and len(props[0x82]) >= 4:
        edt = props[0x82]
        prefix = ""
        if edt[0] > 0:
            prefix += chr(edt[0])
        if edt[1] > 0:
            prefix += chr(edt[1])
        result["standard_version"] = "{}{}.{}".format(prefix, chr(edt[2]), edt[3])

    # 88: fault status (0x41=fault, 0x42=no fault)
    if 0x88 in props and len(props[0x88]) >= 1:
        fault = props[0x88][0]
        if fault == 0x41:
            result["fault_status"] = "fault"
        elif fault == 0x42:
            result["fault_status"] = "normal"
        else:
            result["fault_status"] = "unknown"

    # 8A: maker code (3-byte vendor code)
    if 0x8A in props and len(props[0x8A]) >= 1:
        result["maker_code"] = binascii.hexlify(bytes(props[0x8A])).decode("ascii").upper()

    # 8D: serial number (ASCII)
    if 0x8D in props and len(props[0x8D]) >= 1:
        result["serial_number"] = bytes(props[0x8D]).decode("ascii", "replace").strip().replace("\x00", "")

    # 97: current time setting
    if 0x97 in props and len(props[0x97]) >= 2:
        result["meter_time"] = "{:02d}:{:02d}".format(props[0x97][0], props[0x97][1])

    # 98: current date setting
    if 0x98 in props and len(props[0x98]) >= 4:
        year = struct.unpack(">H", bytes(props[0x98][0:2]))[0]
        result["meter_date"] = "{:04d}-{:02d}-{:02d}".format(year, props[0x98][2], props[0x98][3])

    # D3: coefficient (4-byte unsigned)
    if 0xD3 in props and len(props[0xD3]) >= 4:
        result["coefficient"] = struct.unpack(">I", bytes(props[0xD3][:4]))[0]

    # D7: number of effective digits for cumulative energy
    if 0xD7 in props and len(props[0xD7]) >= 1:
        result["effective_digits"] = int(binascii.hexlify(bytes(props[0xD7])), 16)

    # E1: unit exponent byte
    if 0xE1 in props and len(props[0xE1]) >= 1:
        unit_byte = props[0xE1][0]
        unit_map = {0x00: 1.0, 0x01: 0.1,  0x02: 0.01,   0x03: 0.001, 0x04: 0.0001,
                    0x0A: 10.0, 0x0B: 100.0, 0x0C: 1000.0, 0x0D: 10000.0}
        result["unit_kwh"] = unit_map.get(unit_byte, 1.0)

    # E7: instantaneous power W (4-byte signed)
    if 0xE7 in props and len(props[0xE7]) >= 4:
        result["power_w"] = struct.unpack(">i", bytes(props[0xE7][:4]))[0]

    # E0: cumulative forward kWh (4-byte unsigned × coeff × unit)
    if 0xE0 in props and len(props[0xE0]) >= 4:
        result["energy_forward_raw"] = struct.unpack(">I", bytes(props[0xE0][:4]))[0]

    # E3: cumulative reverse kWh (4-byte unsigned × coeff × unit)
    if 0xE3 in props and len(props[0xE3]) >= 4:
        result["energy_reverse_raw"] = struct.unpack(">I", bytes(props[0xE3][:4]))[0]

    # E8: instantaneous current R,T phase (2×signed short, 0.1A)
    if 0xE8 in props and len(props[0xE8]) >= 4:
        r, t = struct.unpack(">hh", bytes(props[0xE8][:4]))
        result["current_r_a"] = r / 10.0
        result["current_t_a"] = t / 10.0

    # D0: one-minute measured cumulative energy
    if 0xD0 in props and len(props[0xD0]) >= 15:
        dt = decode_datetime7(props[0xD0][0:7])
        result["one_minute_timestamp"] = format_datetime(dt)
        result["one_minute_energy_forward_raw"] = struct.unpack(">I", bytes(props[0xD0][7:11]))[0]
        result["one_minute_energy_reverse_raw"] = struct.unpack(">I", bytes(props[0xD0][11:15]))[0]

    # EA/EB: cumulative energy measured at fixed time
    if 0xEA in props and len(props[0xEA]) >= 11:
        dt = decode_datetime7(props[0xEA][0:7])
        result["fixed_time_forward_timestamp"] = format_datetime(dt)
        result["fixed_time_energy_forward_raw"] = struct.unpack(">I", bytes(props[0xEA][7:11]))[0]

    if 0xEB in props and len(props[0xEB]) >= 11:
        dt = decode_datetime7(props[0xEB][0:7])
        result["fixed_time_reverse_timestamp"] = format_datetime(dt)
        result["fixed_time_energy_reverse_raw"] = struct.unpack(">I", bytes(props[0xEB][7:11]))[0]

    return result

def apply_energy_scale(measurements, coeff, unit_kwh):
    c = measurements.get("coefficient", coeff)
    u = measurements.get("unit_kwh", unit_kwh)
    if "energy_forward_raw" in measurements:
        measurements["energy_forward_kwh"] = measurements["energy_forward_raw"] * c * u
    if "energy_reverse_raw" in measurements:
        measurements["energy_reverse_kwh"] = measurements["energy_reverse_raw"] * c * u
    if measurements.get("one_minute_energy_forward_raw") not in (None, MISSING_CUMULATIVE_ENERGY):
        measurements["one_minute_energy_forward_kwh"] = measurements["one_minute_energy_forward_raw"] * c * u
    if measurements.get("one_minute_energy_reverse_raw") not in (None, MISSING_CUMULATIVE_ENERGY):
        measurements["one_minute_energy_reverse_kwh"] = measurements["one_minute_energy_reverse_raw"] * c * u
    if measurements.get("fixed_time_energy_forward_raw") not in (None, MISSING_CUMULATIVE_ENERGY):
        measurements["fixed_time_energy_forward_kwh"] = measurements["fixed_time_energy_forward_raw"] * c * u
    if measurements.get("fixed_time_energy_reverse_raw") not in (None, MISSING_CUMULATIVE_ENERGY):
        measurements["fixed_time_energy_reverse_kwh"] = measurements["fixed_time_energy_reverse_raw"] * c * u
    return measurements

# ---------------------------------------------------------------------------
# Send ECHONET Lite Get via SKSENDTO
# ---------------------------------------------------------------------------

def send_el_get(fd, ipv6, tid, epcs=None):
    frame = build_el_get(tid, epcs or DEFAULT_EPCS)
    # SKSENDTO expects 4-hex-digit payload length and trailing CRLF after raw data.
    cmd = "SKSENDTO 1 {} 0E1A 1 0 {:04X} ".format(ipv6, len(frame))
    serial_write(fd, cmd)
    serial_write(fd, frame)
    serial_write(fd, b"\r\n")

# Per-property response size estimate (EPC + PDC bytes), used to keep each
# batched Get request within what this meter answers in a single frame.
# Observed in the field: a Get with all polling EPCs in one frame comes
# back as Get_SNA with a silently truncated OPC list (only the first ~6
# properties), instead of an error for the dropped ones - so requests must
# be split into smaller batches rather than sent as one combined Get.
EPC_RESPONSE_BYTES = {
    0x80: 1, 0x81: 1, 0x82: 4, 0x88: 1, 0x8A: 3, 0x8D: 12, 0x97: 2, 0x98: 4,
    0xD0: 15, 0xD3: 4, 0xD7: 1, 0xE0: 4, 0xE1: 1, 0xE2: 4,
    0xE3: 4, 0xE4: 4, 0xE5: 4, 0xE7: 4, 0xE8: 4, 0xEA: 11, 0xEB: 11,
}
MAX_BATCH_RESPONSE_BYTES = 33

def batch_epcs(epcs):
    """Split epcs into request batches sized to fit one Get response frame."""
    batches = []
    current = []
    current_size = 0
    for epc in epcs:
        size = 2 + EPC_RESPONSE_BYTES.get(epc, 4)
        if current and current_size + size > MAX_BATCH_RESPONSE_BYTES:
            batches.append(current)
            current = []
            current_size = 0
        current.append(epc)
        current_size += size
    if current:
        batches.append(current)
    return batches

def read_measurements(fd, ipv6, tid, epcs):
    """Get a list of EPCs, merging results across one or more Get requests.

    Returns (props, tid). epcs is split into size-limited batches by
    batch_epcs() since a single combined Get can lose properties beyond
    what the meter answers in one frame.
    """
    props = {}
    for batch in batch_epcs(epcs):
        send_el_get(fd, ipv6, tid, batch)
        request_tid = tid
        tid = (tid + 1) & 0xFFFF
        data = read_erxudp(fd, timeout=15, expected_tid=request_tid)
        if data:
            props.update(parse_el_response(data))
        else:
            log("No ERXUDP response (timeout) for batch {}".format(format_epcs(batch)))
    return props, tid

def detect_poll_epcs(fd, ipv6, tid):
    """Get the property map and derive poll_epcs; returns (poll_epcs, tid).

    Retries a few times before falling back to DEFAULT_EPCS, since a
    single timeout/empty response is often transient (radio noise, a
    busy meter) rather than the meter actually lacking a property map.
    """
    for attempt in range(1, PROPERTY_MAP_MAX_RETRIES + 1):
        send_el_get(fd, ipv6, tid, [PROPERTY_MAP_EPC])
        request_tid = tid
        tid = (tid + 1) & 0xFFFF
        data = read_erxudp(fd, timeout=15, expected_tid=request_tid)
        if data:
            props = parse_el_response(data)
            if PROPERTY_MAP_EPC in props:
                supported = parse_property_map(props[PROPERTY_MAP_EPC])
                log("Gettable EPCs: {}".format(format_epcs(supported)))
                poll_epcs = list(DEFAULT_EPCS)
                for epc in EXTRA_EPCS:
                    if epc in supported and epc not in poll_epcs:
                        poll_epcs.append(epc)
                log("Polling EPCs: {}".format(format_epcs(poll_epcs)))
                write_status(gettable_epcs=epcs_to_hex(sorted(supported)),
                             polling_epcs=epcs_to_hex(poll_epcs),
                             last_error="")
                return poll_epcs, tid
            log("Get property map unavailable (attempt {}/{})".format(
                attempt, PROPERTY_MAP_MAX_RETRIES))
        else:
            log("Get property map timeout (attempt {}/{})".format(
                attempt, PROPERTY_MAP_MAX_RETRIES))
        if attempt < PROPERTY_MAP_MAX_RETRIES:
            time.sleep(PROPERTY_MAP_RETRY_DELAY)

    log("Get property map failed after {} attempts; polling default EPCs only".format(
        PROPERTY_MAP_MAX_RETRIES))
    write_status(gettable_epcs=[],
                 polling_epcs=epcs_to_hex(DEFAULT_EPCS),
                 last_error="Get property map failed after retries")
    return list(DEFAULT_EPCS), tid

def reconnect_wisun(fd, br_id, br_pwd, tid, poll_epcs):
    """Re-join Wi-SUN and re-detect polling EPCs.

    Raises if wisun_connect() itself fails. If EPC re-detection fails,
    logs it and keeps the previously known poll_epcs.
    """
    ipv6 = wisun_connect(fd, br_id, br_pwd)
    log("Wi-SUN reconnected at {}".format(ipv6))
    write_status(wisun_connected=True,
                 meter_ipv6=ipv6,
                 last_error="")
    try:
        poll_epcs, tid = detect_poll_epcs(fd, ipv6, tid)
    except Exception as e3:
        log("EPC detection after reconnect failed: {} - keep previous polling EPCs".format(e3))
        write_status(last_error="EPC detection after reconnect failed: {}".format(e3))
    return ipv6, poll_epcs, tid

def read_erxudp(fd, timeout=15, expected_tid=None):
    """Wait for ERXUDP and return payload as bytearray, or None.

    If expected_tid is given, frames whose ECHONET Lite TID doesn't
    match are skipped instead of being returned. Without this, a
    delayed or duplicated response to an earlier request (the meter
    is known to retransmit) can be mistaken for the answer to the
    current one, e.g. a stale property-map reply consumed by the next
    measurement poll.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = serial_readline(fd, timeout=max(0.5, deadline - time.time()))
        if line is None:
            continue
        if line.startswith("ERXUDP"):
            parts = line.split()
            # Tail fields are stable: ... <secured> <side> <datalen> <data>
            if len(parts) >= 10:
                hex_data = parts[-1].strip()
                if not hex_data.startswith("1081"):
                    continue
                try:
                    frame = bytearray(binascii.unhexlify(hex_data))
                except Exception as e:
                    log("ERXUDP hex decode error: {}".format(e))
                    continue
                if expected_tid is not None and len(frame) >= 4:
                    frame_tid = struct.unpack(">H", bytes(frame[2:4]))[0]
                    if frame_tid != expected_tid:
                        log("ERXUDP TID mismatch (got {:04X}, expected {:04X}) - ignoring stale response".format(
                            frame_tid, expected_tid))
                        continue
                return frame
    return None

# ---------------------------------------------------------------------------
# Minimal MQTT 3.1.1 client (raw socket, no paho)
# ---------------------------------------------------------------------------

def _encode_remaining(n):
    buf = b""
    while True:
        byte = n % 128
        n //= 128
        if n > 0:
            byte |= 0x80
        buf += struct.pack("B", byte)
        if n == 0:
            break
    return buf

def _encode_str(s):
    b = s.encode("utf-8")
    return struct.pack(">H", len(b)) + b

MQTT_OUT_QUEUE_MAX = 200
PING_RESPONSE_TIMEOUT = 20

def _byte_at(buf, index):
    b = buf[index]
    return b if isinstance(b, int) else ord(b)

class MQTTClient(object):
    def __init__(self, host, port, client_id, username=None, password=None,
                 will_topic=None, will_payload=None, will_retain=False,
                 subscriptions=None, on_message=None):
        self.host      = host
        self.port      = port
        self.client_id = client_id
        self.username  = username
        self.password  = password
        self.will_topic   = will_topic
        self.will_payload = will_payload
        self.will_retain  = will_retain
        self.subscriptions = list(subscriptions or [])
        self.on_message    = on_message
        self.sock      = None
        self._out_queue = collections.deque()
        self._send_lock = threading.Lock()
        self._packet_id = 0
        # Set when a PINGREQ has gone out and its PINGRESP is still
        # outstanding; cleared by the receive thread.
        self._pending_ping_since = None

    def start(self):
        """Connect in the background and keep reconnecting on failure.

        Wi-SUN polling must not stall just because MQTT is unreachable,
        so the actual (re)connect attempts run on their own thread;
        publish()/ping() only ever touch a socket that already exists
        and otherwise queue/no-op instead of blocking the caller.
        """
        t = threading.Thread(target=self._keepalive_loop)
        t.daemon = True
        t.start()

    def _keepalive_loop(self):
        while True:
            if self.sock is None:
                try:
                    self.connect()
                except Exception as e:
                    log("MQTT connect failed: {} - retry in 15s".format(e))
                    write_status(mqtt_connected=False,
                                 last_error="MQTT connect failed: {}".format(e))
                    time.sleep(15)
                    continue
            time.sleep(5)

    def _drop_connection(self, reason):
        log("MQTT connection dropped: {}".format(reason))
        write_status(mqtt_connected=False,
                     last_error="MQTT error: {}".format(reason))
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        self.sock = None

    def _queue(self, topic, payload, retain):
        self._out_queue.append((topic, payload, retain))
        while len(self._out_queue) > MQTT_OUT_QUEUE_MAX:
            self._out_queue.popleft()

    def connect(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(30)
        # Enable TCP keepalive where available
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            # platform-specific options
            for opt_name, opt_val in (('TCP_KEEPIDLE', 60), ('TCP_KEEPINTVL', 10), ('TCP_KEEPCNT', 3)):
                if hasattr(socket, opt_name):
                    try:
                        s.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt_name), opt_val)
                    except Exception:
                        pass
        except Exception:
            pass

        s.connect((self.host, self.port))

        flags = 0x02  # clean session
        if self.username: flags |= 0x80
        if self.password: flags |= 0x40
        if self.will_topic:
            flags |= 0x04
            if self.will_retain:
                flags |= 0x20

        var_hdr = (b"\x00\x04MQTT"
                   + b"\x04"
                   + struct.pack("B", flags)
                   + b"\x00\x3C")   # keep-alive 60s

        # Payload order per MQTT 3.1.1 spec: client id, then will
        # topic/message (if the will flag is set), then user/password.
        payload = _encode_str(self.client_id)
        if self.will_topic:
            payload += _encode_str(self.will_topic)
            payload += _encode_str(self.will_payload or "")
        if self.username: payload += _encode_str(self.username)
        if self.password: payload += _encode_str(self.password)

        remaining = var_hdr + payload
        pkt = b"\x10" + _encode_remaining(len(remaining)) + remaining
        s.sendall(pkt)

        # read CONNACK
        s.settimeout(10)
        ack = b""
        while len(ack) < 4:
            chunk = s.recv(4 - len(ack))
            if not chunk:
                break
            ack += chunk
        s.settimeout(None)

        if len(ack) < 4 or (ack[0] if isinstance(ack[0], int) else ord(ack[0])) != 0x20:
            raise RuntimeError("MQTT: bad CONNACK ({})".format(binascii.hexlify(ack)))
        rc = ack[3] if isinstance(ack[3], int) else ord(ack[3])
        if rc != 0:
            raise RuntimeError("MQTT: connection refused code {}".format(rc))

        self.sock = s
        self._pending_ping_since = None
        log("MQTT connected to {}:{}".format(self.host, self.port))
        write_status(mqtt_connected=True,
                     mqtt_host=self.host,
                     mqtt_port=self.port,
                     last_error="")

        # The receive thread is bound to this specific socket, so a later
        # reconnect's thread can never race the old one over self.sock.
        rx = threading.Thread(target=self._recv_loop, args=(s,))
        rx.daemon = True
        rx.start()

        self._subscribe_all()

        # Republish "online" on every (re)connect - the broker only sends
        # our will message ("offline") on an ungraceful disconnect, so the
        # retained availability topic must be refreshed back to "online"
        # each time we successfully reconnect.
        if self.will_topic:
            self.publish(self.will_topic, "online", retain=True)

        # flush any queued messages
        try:
            self._flush_queue()
        except Exception as e:
            log("MQTT flush queue error: {}".format(e))

    def _make_pkt(self, topic, payload, retain=False):
        if isinstance(payload, dict):
            payload = json.dumps(payload, separators=(",", ":"))
        topic_b = topic.encode("utf-8")
        payload_b = payload.encode("utf-8") if isinstance(payload, str) else payload
        fixed = 0x30 | (0x01 if retain else 0x00)
        var_hdr = struct.pack(">H", len(topic_b)) + topic_b
        remaining = var_hdr + payload_b
        return struct.pack("B", fixed) + _encode_remaining(len(remaining)) + remaining

    def _send_raw(self, pkt):
        # Serialized: the connect thread publishes "online"/flushes the queue
        # while the main loop may be publishing measurements, and two
        # interleaved sendall() calls would corrupt the packet stream.
        sock = self.sock
        if not sock:
            raise RuntimeError("MQTT: not connected")
        with self._send_lock:
            sock.sendall(pkt)

    def publish(self, topic, payload, retain=False):
        if not self.sock:
            self._queue(topic, payload, retain)
            return
        try:
            self._send_raw(self._make_pkt(topic, payload, retain))
        except Exception as e:
            log("MQTT publish error: {}".format(e))
            self._drop_connection(e)
            self._queue(topic, payload, retain)

    def _flush_queue(self):
        while self._out_queue and self.sock:
            topic, payload, retain = self._out_queue[0]
            try:
                self._send_raw(self._make_pkt(topic, payload, retain))
                self._out_queue.popleft()
            except Exception as e:
                log("MQTT queued publish failed: {}".format(e))
                break

    def _subscribe_all(self):
        if not self.subscriptions:
            return
        self._packet_id = (self._packet_id % 65535) + 1
        payload = struct.pack(">H", self._packet_id)
        for topic in self.subscriptions:
            payload += _encode_str(topic) + b"\x00"   # requested QoS 0
        self._send_raw(b"\x82" + _encode_remaining(len(payload)) + payload)
        log("MQTT subscribe: {}".format(", ".join(self.subscriptions)))

    def _recv_exact(self, sock, n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    def _recv_remaining_length(self, sock):
        multiplier = 1
        value = 0
        for _ in range(4):
            chunk = self._recv_exact(sock, 1)
            if chunk is None:
                return None
            byte = _byte_at(chunk, 0)
            value += (byte & 0x7F) * multiplier
            if not byte & 0x80:
                return value
            multiplier *= 128
        return None

    def _recv_loop(self, sock):
        """Read packets off `sock` until it dies, dispatching to on_message.

        Everything the broker sends has to be drained here: before
        subscriptions existed, ping() could get away with recv()ing its
        own PINGRESP inline, but an inbound PUBLISH arriving at the wrong
        moment would have been mistaken for one.
        """
        try:
            while True:
                header = self._recv_exact(sock, 1)
                if header is None:
                    break
                first_byte = _byte_at(header, 0)
                remaining = self._recv_remaining_length(sock)
                if remaining is None:
                    break
                body = self._recv_exact(sock, remaining) if remaining else b""
                if body is None:
                    break
                packet_type = first_byte & 0xF0
                if packet_type == 0xD0:      # PINGRESP
                    self._pending_ping_since = None
                elif packet_type == 0x30:    # PUBLISH
                    self._dispatch_publish(first_byte, body)
        except Exception as e:
            log("MQTT receive error: {}".format(e))
        # A connection we already tore down elsewhere must not be reported twice.
        if self.sock is sock:
            self._drop_connection("connection closed by broker")

    def _dispatch_publish(self, first_byte, body):
        if len(body) < 2:
            return
        topic_len = struct.unpack(">H", bytes(body[:2]))[0]
        pos = 2 + topic_len
        if len(body) < pos:
            return
        topic = body[2:pos].decode("utf-8", "replace")
        if (first_byte >> 1) & 0x03:
            pos += 2   # packet identifier; only present above QoS 0
        payload = body[pos:].decode("utf-8", "replace")
        if not self.on_message:
            return
        try:
            self.on_message(topic, payload)
        except Exception as e:
            log("MQTT on_message error: {}".format(e))

    def ping(self):
        if not self.sock:
            return
        if (self._pending_ping_since is not None and
                time.time() - self._pending_ping_since > PING_RESPONSE_TIMEOUT):
            self._drop_connection("PINGRESP timeout ({}s)".format(PING_RESPONSE_TIMEOUT))
            return
        try:
            self._send_raw(b"\xC0\x00")
            if self._pending_ping_since is None:
                self._pending_ping_since = time.time()
        except Exception as e:
            log("MQTT ping error: {}".format(e))
            self._drop_connection(e)

# ---------------------------------------------------------------------------
# Home Assistant MQTT auto-discovery
# ---------------------------------------------------------------------------

SENSOR_DEFS = [
    ("power",                         "Instantaneous Power",       "W",   "power",   "measurement"),
    ("energy_forward",                "Cumulative Energy Fwd",     "kWh", "energy",  "total_increasing"),
    ("energy_reverse",                "Cumulative Energy Rev",     "kWh", "energy",  "total_increasing"),
    ("current_r",                     "Current R Phase",           "A",   "current", "measurement"),
    ("current_t",                     "Current T Phase",           "A",   "current", "measurement"),
    ("one_minute_energy_forward",     "One Minute Energy Fwd",     "kWh", "energy",  "total_increasing"),
    ("one_minute_energy_reverse",     "One Minute Energy Rev",     "kWh", "energy",  "total_increasing"),
    ("fixed_time_energy_forward",     "Fixed Time Energy Fwd",     "kWh", "energy",  "total_increasing"),
    ("fixed_time_energy_reverse",     "Fixed Time Energy Rev",     "kWh", "energy",  "total_increasing"),
    ("effective_digits",              "Cumulative Energy Digits",  None,  None,      None),
    ("operation_status",              "Operation Status",          None,  None,      None),
    ("fault_status",                  "Fault Status",              None,  None,      None),
    ("standard_version",              "Standard Version",          None,  None,      None),
    ("meter_date",                    "Meter Date",                None,  None,      None),
    ("meter_time",                    "Meter Time",                None,  None,      None),
    ("one_minute_timestamp",          "One Minute Timestamp",      None,  None,      None),
    ("fixed_time_forward_timestamp",  "Fixed Time Fwd Timestamp",  None,  None,      None),
    ("fixed_time_reverse_timestamp",  "Fixed Time Rev Timestamp",  None,  None,      None),
    ("installation_place",            "Installation Place",        None,  None,      None),
    ("maker_code",                    "Maker Code",                None,  None,      None),
    ("serial_number",                 "Serial Number",             None,  None,      None),
]

# Buttons that trigger an immediate read instead of waiting out poll_interval.
# Each is (id, label, primary EPCs, support EPCs); primary None means "whatever
# the regular poll asks for". The split matters: D3/E1 are only fetched so the
# kWh conversion has a current coefficient and unit, so a button whose primary
# EPCs are all unsupported is useless even though D3/E1 would still resolve.
BUTTON_DEFS = [
    ("refresh_instant",    "Refresh Instantaneous Values", [0xE7, 0xE8],             []),
    ("refresh_cumulative", "Refresh Cumulative Energy",    [0xE0, 0xE3, 0xEA, 0xEB], [0xD3, 0xE1]),
    ("refresh_one_minute", "Refresh One Minute Energy",    [0xD0],                   [0xD3, 0xE1]),
    ("refresh_all",        "Refresh All",                  None,                     []),
]
BUTTON_ACTIONS = dict((bid, (primary, support)) for bid, _, primary, support in BUTTON_DEFS)

# A press is answered on the next pass through the main loop, but no faster
# than this - the Wi-SUN duty cycle doesn't survive an automation that
# presses a button in a tight loop.
ON_DEMAND_MIN_INTERVAL = 10
ON_DEMAND_QUEUE_MAX = 4

def command_topic(device_id, action):
    return "cubej/{}/command/{}".format(device_id, action)

def command_topic_filter(device_id):
    return "cubej/{}/command/+".format(device_id)

def resolve_button_epcs(action, poll_epcs):
    """EPCs a button press should read, restricted to what the meter supports.

    Empty means the button has nothing to offer on this meter - either the
    action is unknown, or none of its primary EPCs are in the property map.
    """
    entry = BUTTON_ACTIONS.get(action)
    if entry is None:
        return []
    primary, support = entry
    if primary is None:
        return list(poll_epcs)
    wanted = [epc for epc in primary if epc in poll_epcs]
    if not wanted:
        return []
    return [epc for epc in support if epc in poll_epcs] + wanted

def publish_button_discovery(mqtt, device_id, poll_epcs):
    """(Re)publish button discovery, dropping buttons this meter can't serve.

    Called after the property map is known rather than at startup so a
    meter without, say, D0 doesn't get a One Minute button that could
    only ever no-op. Unsupported ones are cleared with an empty retained
    payload in case an earlier run did publish them.
    """
    device = {
        "identifiers": [device_id],
        "name":         "Cube J1 Smart Meter",
        "model":        "Cube J1",
        "manufacturer": "NextDrive",
    }
    availability_topic = "cubej/{}/status".format(device_id)
    for bid, name, _, _ in BUTTON_DEFS:
        topic = "homeassistant/button/{}/{}/config".format(device_id, bid)
        if not resolve_button_epcs(bid, poll_epcs):
            mqtt.publish(topic, "", retain=True)
            log("HA discovery: removed unsupported button {}".format(bid))
            continue
        mqtt.publish(topic, {
            "name":               name,
            "unique_id":          "{}_{}".format(device_id, bid),
            "command_topic":      command_topic(device_id, bid),
            "payload_press":      "PRESS",
            "availability_topic": availability_topic,
            "payload_available":  "online",
            "payload_not_available": "offline",
            "device":             device,
        }, retain=True)
        log("HA discovery: {}".format(topic))

def publish_ha_discovery(mqtt, device_id):
    device = {
        "identifiers": [device_id],
        "name":         "Cube J1 Smart Meter",
        "model":        "Cube J1",
        "manufacturer": "NextDrive",
    }
    base = "cubej/{}".format(device_id)
    availability_topic = "{}/status".format(base)
    for sid, name, unit, dev_class, state_class in SENSOR_DEFS:
        topic  = "homeassistant/sensor/{}/{}/config".format(device_id, sid)
        config = {
            "name":               name,
            "unique_id":          "{}_{}".format(device_id, sid),
            "state_topic":        "{}/{}".format(base, sid),
            "availability_topic": availability_topic,
            "payload_available":  "online",
            "payload_not_available": "offline",
            "device":             device,
        }
        if unit:
            config["unit_of_measurement"] = unit
        if dev_class:
            config["device_class"] = dev_class
        if state_class:
            config["state_class"] = state_class
        mqtt.publish(topic, config, retain=True)
        log("HA discovery: {}".format(topic))

def publish_measurements(mqtt, device_id, m):
    base = "cubej/{}".format(device_id)
    if "power_w" in m:
        mqtt.publish("{}/power".format(base), str(m["power_w"]))
    if "energy_forward_kwh" in m:
        mqtt.publish("{}/energy_forward".format(base), "{:.3f}".format(m["energy_forward_kwh"]))
    if "energy_reverse_kwh" in m:
        mqtt.publish("{}/energy_reverse".format(base), "{:.3f}".format(m["energy_reverse_kwh"]))
    if "current_r_a" in m:
        mqtt.publish("{}/current_r".format(base), "{:.1f}".format(m["current_r_a"]))
    if "current_t_a" in m:
        mqtt.publish("{}/current_t".format(base), "{:.1f}".format(m["current_t_a"]))
    if "one_minute_energy_forward_kwh" in m:
        mqtt.publish("{}/one_minute_energy_forward".format(base), "{:.3f}".format(m["one_minute_energy_forward_kwh"]))
    if "one_minute_energy_reverse_kwh" in m:
        mqtt.publish("{}/one_minute_energy_reverse".format(base), "{:.3f}".format(m["one_minute_energy_reverse_kwh"]))
    if "fixed_time_energy_forward_kwh" in m:
        mqtt.publish("{}/fixed_time_energy_forward".format(base), "{:.3f}".format(m["fixed_time_energy_forward_kwh"]))
    if "fixed_time_energy_reverse_kwh" in m:
        mqtt.publish("{}/fixed_time_energy_reverse".format(base), "{:.3f}".format(m["fixed_time_energy_reverse_kwh"]))
    if "effective_digits" in m:
        mqtt.publish("{}/effective_digits".format(base), str(m["effective_digits"]))
    for key in ("operation_status", "fault_status", "standard_version", "meter_date",
                "meter_time", "one_minute_timestamp", "fixed_time_forward_timestamp",
                "fixed_time_reverse_timestamp", "installation_place", "maker_code",
                "serial_number"):
        if key in m and m[key] is not None:
            mqtt.publish("{}/{}".format(base, key), str(m[key]))

def publish_bridge_status(mqtt, device_id):
    base = "cubej/{}".format(device_id)
    bridge_payload = {
        "updated_at": now_str(),
        "configuration_required": _status.get("configuration_required", False),
        "missing_config": _status.get("missing_config", []),
        "mqtt_connected": _status.get("mqtt_connected"),
        "wisun_connected": _status.get("wisun_connected"),
        "last_error": _status.get("last_error", ""),
        "last_measurement_at": _status.get("last_measurement_at"),
    }
    ota_payload = load_ota_status()
    mqtt.publish("{}/bridge_status".format(base), bridge_payload, retain=True)
    mqtt.publish("{}/ota_status".format(base), ota_payload, retain=True)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def wait_for_next_poll(poll_interval, command_queue, command_event, last_on_demand):
    """Block until the next scheduled poll is due, or a button press arrives.

    Returns the queued action name, or None when poll_interval simply
    elapsed. Presses that come in faster than ON_DEMAND_MIN_INTERVAL stay
    queued until the rate limit lets them through (or until the regular
    poll overtakes them, which serves the same data anyway).
    """
    deadline = time.time() + poll_interval
    while True:
        # Cleared before looking at the queue, never after: a press landing
        # between the check and the clear would otherwise have its wakeup
        # swallowed and sit unserved until the next scheduled poll.
        command_event.clear()
        now = time.time()
        if command_queue:
            ready_at = last_on_demand[0] + ON_DEMAND_MIN_INTERVAL
            if now >= ready_at:
                last_on_demand[0] = now
                return command_queue.popleft()
            timeout = min(ready_at - now, deadline - now)
        else:
            timeout = deadline - now
        if timeout <= 0:
            return None
        command_event.wait(timeout)

def sync_poll_epcs(mqtt, device_id, poll_epcs, poll_epcs_ref):
    """Publish the polling EPCs to the command handler and the button set.

    The supported-EPC set is re-derived on every reconnect, so both the
    handler's accept list and HA's buttons have to follow it rather than
    being fixed at startup.
    """
    poll_epcs_ref[0] = poll_epcs
    publish_button_discovery(mqtt, device_id, poll_epcs)

def make_command_handler(device_id, command_queue, command_event, poll_epcs_ref):
    """Build the MQTT callback that turns button presses into poll requests.

    Runs on the MQTT receive thread, so it only ever enqueues - the serial
    port stays owned by the main loop.
    """
    prefix = "cubej/{}/command/".format(device_id)

    def on_message(topic, payload):
        if not topic.startswith(prefix):
            log("Ignoring message on unexpected topic: {}".format(topic))
            return
        action = topic[len(prefix):]
        if action not in BUTTON_ACTIONS:
            log("Ignoring unknown command: {}".format(action))
            return
        if not resolve_button_epcs(action, poll_epcs_ref[0]):
            log("Ignoring command {}: this meter supports none of its EPCs".format(action))
            return
        if action in command_queue:
            return   # already pending; pressing twice fetches the same values
        if len(command_queue) >= ON_DEMAND_QUEUE_MAX:
            log("On-demand queue is full - dropping {}".format(action))
            return
        command_queue.append(action)
        command_event.set()
        log("On-demand request: {} (payload={})".format(action, payload.strip()[:32]))

    return on_message

def main():
    global _log_file, _serial_log_file, LOG_MAX_BYTES, PAN_CACHE_ENABLED
    try:
        _log_file = open(LOG_PATH, "a")
    except Exception:
        pass
    try:
        _serial_log_file = open(SERIAL_LOG_PATH, "a")
    except Exception:
        pass

    write_status(bridge_started_at=now_str(),
                 configuration_required=True,
                 mqtt_connected=False,
                 wisun_connected=False,
                 last_error="Loading configuration")

    cfg           = wait_for_config()
    LOG_MAX_BYTES = int(cfg.get("log_max_bytes", LOG_MAX_BYTES))
    br_id         = cfg["br_id"]
    br_pwd        = cfg["br_pwd"]
    ha_host       = cfg["mqtt_host"]
    ha_port       = int(cfg.get("mqtt_port", 1883))
    ha_user       = cfg.get("mqtt_user", "")
    ha_pass       = cfg.get("mqtt_pass", "")
    device_id     = cfg.get("device_id", "cubej1")
    serial_port   = cfg.get("serial_port", "/dev/ttyS1")
    poll_interval = int(cfg.get("poll_interval", 60))
    PAN_CACHE_ENABLED = bool(cfg.get("pan_cache_enabled", PAN_CACHE_ENABLED))
    if not PAN_CACHE_ENABLED:
        # Drop any existing entry now rather than at re-enable time, so
        # turning the cache back on later can't resurrect a stale PAN.
        clear_pan_cache("pan_cache_enabled is off")

    log("=== mqtt_bridge start device_id={} ===".format(device_id))
    write_status(bridge_started_at=now_str(),
                 device_id=device_id,
                 mqtt_host=ha_host,
                 mqtt_port=ha_port,
                 serial_port=serial_port,
                 poll_interval=poll_interval,
                 configuration_required=False,
                 missing_config=[],
                 mqtt_connected=False,
                 wisun_connected=False,
                 meter_ipv6=None,
                 wisun_channel=None,
                 wisun_pan_id=None,
                 wisun_lqi=None,
                 wisun_pan_source=None,
                 pan_cache_enabled=PAN_CACHE_ENABLED,
                 gettable_epcs=[],
                 polling_epcs=epcs_to_hex(DEFAULT_EPCS),
                 last_measurement_at=None,
                 last_values={},
                 last_error="")

    # Button presses arrive on the MQTT thread and are handed to the main
    # loop through this queue; poll_epcs_ref lets the handler reject actions
    # the meter can't serve without reaching into the loop's locals.
    command_queue = collections.deque()
    command_event = threading.Event()
    poll_epcs_ref = [list(DEFAULT_EPCS)]
    last_on_demand = [0.0]

    # Connect MQTT in the background; Wi-SUN setup below must not wait on it.
    # A will (LWT) is registered so Home Assistant marks entities
    # "unavailable" if the bridge disappears without a clean disconnect.
    status_topic = "cubej/{}/status".format(device_id)
    mqtt = MQTTClient(ha_host, ha_port, "cubej1_{}".format(device_id),
                      username=ha_user, password=ha_pass,
                      will_topic=status_topic, will_payload="offline", will_retain=True,
                      subscriptions=[command_topic_filter(device_id)],
                      on_message=make_command_handler(device_id, command_queue,
                                                      command_event, poll_epcs_ref))
    mqtt.start()

    publish_ha_discovery(mqtt, device_id)
    publish_bridge_status(mqtt, device_id)

    # Open serial port
    log("Opening serial {}".format(serial_port))
    fd = None
    while True:
        try:
            fd = open_serial(serial_port)
            break
        except Exception as e:
            log("Serial open failed: {} - retry in 10s".format(e))
            write_status(last_error="Serial open failed: {}".format(e))
            time.sleep(10)

    # Wi-SUN join
    ipv6 = None
    while True:
        try:
            ipv6 = wisun_connect(fd, br_id, br_pwd)
            break
        except Exception as e:
            log("Wi-SUN join failed: {} - retry in 60s".format(e))
            write_status(wisun_connected=False,
                         last_error="Wi-SUN join failed: {}".format(e))
            time.sleep(60)

    log("Meter connected at {}".format(ipv6))
    write_status(wisun_connected=True,
                 meter_ipv6=ipv6,
                 last_error="")

    tid       = 1
    coeff     = 1
    unit_kwh  = 1.0
    last_ping = time.time()
    last_status_publish = 0
    consecutive_timeouts = 0
    try:
        poll_epcs, tid = detect_poll_epcs(fd, ipv6, tid)
    except Exception as e:
        log("EPC detection failed: {} - polling default EPCs only".format(e))
        poll_epcs = list(DEFAULT_EPCS)
    sync_poll_epcs(mqtt, device_id, poll_epcs, poll_epcs_ref)

    next_action = None
    while True:
        try:
            if next_action:
                epcs = resolve_button_epcs(next_action, poll_epcs)
                log("On-demand poll ({}): {}".format(next_action, format_epcs(epcs)))
            else:
                epcs = poll_epcs

            orig_led = led_read()
            led_rgb(0, 0, 255)
            try:
                props, tid = read_measurements(fd, ipv6, tid, epcs)
                if props:
                    m     = decode_measurements(props)
                    m     = apply_energy_scale(m, coeff, unit_kwh)
                    if "coefficient" in m:
                        coeff = m["coefficient"]
                    if "unit_kwh" in m:
                        unit_kwh = m["unit_kwh"]
                    log("Measurements: {}".format(
                        {k: v for k, v in m.items()
                         if k in ("power_w", "energy_forward_kwh", "energy_reverse_kwh",
                                   "current_r_a", "current_t_a",
                                   "one_minute_energy_forward_kwh", "one_minute_energy_reverse_kwh",
                                   "fixed_time_energy_forward_kwh", "fixed_time_energy_reverse_kwh",
                                   "operation_status", "fault_status")}))
                    write_status(last_measurement_at=now_str(),
                                 last_values=measurement_summary(m),
                                 wisun_connected=True,
                                 last_error="")
                    publish_measurements(mqtt, device_id, m)
                    consecutive_timeouts = 0
                else:
                    consecutive_timeouts += 1
                    log("No ERXUDP response (timeout) ({}/{})".format(
                        consecutive_timeouts, MAX_CONSECUTIVE_TIMEOUTS))
                    write_status(last_error="No ERXUDP response (timeout)")
            finally:
                led_rgb(*orig_led)

            if consecutive_timeouts >= MAX_CONSECUTIVE_TIMEOUTS:
                log("{} consecutive timeouts - forcing Wi-SUN reconnect".format(consecutive_timeouts))
                try:
                    ipv6, poll_epcs, tid = reconnect_wisun(fd, br_id, br_pwd, tid, poll_epcs)
                except Exception as e2:
                    log("Wi-SUN reconnect after repeated timeouts failed: {}".format(e2))
                    write_status(wisun_connected=False,
                                 last_error="Wi-SUN reconnect failed: {}".format(e2))
                sync_poll_epcs(mqtt, device_id, poll_epcs, poll_epcs_ref)
                consecutive_timeouts = 0

            if time.time() - last_ping > 50:
                mqtt.ping()
                last_ping = time.time()

            if time.time() - last_status_publish > 60:
                publish_bridge_status(mqtt, device_id)
                last_status_publish = time.time()

            next_action = wait_for_next_poll(poll_interval, command_queue,
                                             command_event, last_on_demand)

        except Exception as e:
            log("Main loop error: {} - reconnecting Wi-SUN in 30s".format(e))
            write_status(wisun_connected=False,
                         last_error="Main loop error: {}".format(e))
            consecutive_timeouts = 0
            next_action = None
            try:
                publish_bridge_status(mqtt, device_id)
                last_status_publish = time.time()
            except Exception as e_pub:
                log("Status publish failed: {}".format(e_pub))
            time.sleep(30)
            try:
                ipv6, poll_epcs, tid = reconnect_wisun(fd, br_id, br_pwd, tid, poll_epcs)
            except Exception as e2:
                log("Wi-SUN reconnect failed: {}".format(e2))
                write_status(wisun_connected=False,
                             last_error="Wi-SUN reconnect failed: {}".format(e2))
            sync_poll_epcs(mqtt, device_id, poll_epcs, poll_epcs_ref)


if __name__ == "__main__":
    main()
