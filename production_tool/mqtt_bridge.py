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
STATUS_PATH = "/data/local/mqtt_status.json"
OTA_STATUS_PATH = "/data/local/ota_status.json"

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
_status = {}

def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = "[{}] {}\n".format(ts, msg)
    global _log_file
    if _log_file:
        try:
            _log_file.write(line)
            _log_file.flush()
        except Exception:
            pass
    else:
        sys.stderr.write(line)
        sys.stderr.flush()

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

def now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")

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
            "operation_status", "fault_status", "meter_date", "meter_time")
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
            return buf[:-2].decode("ascii", errors="replace")
    return buf.decode("ascii", errors="replace") if buf else None

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

    log("SKSCAN (may take up to 60s)")
    pan = skscan(fd)
    if not pan.get("Channel") or not pan.get("Pan ID") or not pan.get("Addr"):
        raise RuntimeError("SKSCAN: no PAN found ({})".format(pan))

    channel = pan["Channel"]
    pan_id  = pan["Pan ID"]
    mac     = pan["Addr"]
    log("PAN found: ch={} panId={} mac={}".format(channel, pan_id, mac))

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

# ---------------------------------------------------------------------------
# ECHONET Lite frame builder / parser
# ---------------------------------------------------------------------------

DEFAULT_EPCS = [0xD3, 0xE1, 0xE7, 0xE0, 0xE3, 0xE8]
EXTRA_EPCS = [0x80, 0x82, 0x88, 0x97, 0x98, 0xD0, 0xD7, 0xEA, 0xEB]
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

def detect_poll_epcs(fd, ipv6, tid):
    send_el_get(fd, ipv6, tid, [PROPERTY_MAP_EPC])
    data = read_erxudp(fd, timeout=15)
    if not data:
        log("Get property map timeout; polling default EPCs only")
        write_status(gettable_epcs=[],
                     polling_epcs=epcs_to_hex(DEFAULT_EPCS),
                     last_error="Get property map timeout")
        return list(DEFAULT_EPCS)

    props = parse_el_response(data)
    if PROPERTY_MAP_EPC not in props:
        log("Get property map unavailable; polling default EPCs only")
        write_status(gettable_epcs=[],
                     polling_epcs=epcs_to_hex(DEFAULT_EPCS),
                     last_error="Get property map unavailable")
        return list(DEFAULT_EPCS)

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
    return poll_epcs

def read_erxudp(fd, timeout=15):
    """Wait for ERXUDP and return payload as bytearray, or None."""
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
                    return bytearray(binascii.unhexlify(hex_data))
                except Exception as e:
                    log("ERXUDP hex decode error: {}".format(e))
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

class MQTTClient(object):
    def __init__(self, host, port, client_id, username=None, password=None):
        self.host      = host
        self.port      = port
        self.client_id = client_id
        self.username  = username
        self.password  = password
        self.sock      = None
        self._out_queue = collections.deque()

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

        var_hdr = (b"\x00\x04MQTT"
                   + b"\x04"
                   + struct.pack("B", flags)
                   + b"\x00\x3C")   # keep-alive 60s

        payload = _encode_str(self.client_id)
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
        log("MQTT connected to {}:{}".format(self.host, self.port))
        write_status(mqtt_connected=True,
                     mqtt_host=self.host,
                     mqtt_port=self.port,
                     last_error="")

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

    def publish(self, topic, payload, retain=False):
        pkt = self._make_pkt(topic, payload, retain)
        try:
            if not self.sock:
                raise RuntimeError("No MQTT socket")
            self.sock.sendall(pkt)
            return
        except Exception as e:
            log("MQTT publish error: {}".format(e))
            write_status(mqtt_connected=False,
                         last_error="MQTT publish error: {}".format(e))
            # try reconnect and resend
            try:
                self._reconnect()
            except Exception as e2:
                log("MQTT reconnect failed after publish error: {}".format(e2))
                # queue the message for later delivery
                try:
                    self._out_queue.append((topic, payload, retain))
                except Exception:
                    pass
                return

            try:
                self.sock.sendall(pkt)
                return
            except Exception as e3:
                log("MQTT publish retry failed: {}".format(e3))
                try:
                    self._out_queue.append((topic, payload, retain))
                except Exception:
                    pass

    def _flush_queue(self):
        while self._out_queue and self.sock:
            topic, payload, retain = self._out_queue[0]
            try:
                pkt = self._make_pkt(topic, payload, retain)
                self.sock.sendall(pkt)
                self._out_queue.popleft()
            except Exception as e:
                log("MQTT queued publish failed: {}".format(e))
                break

    def ping(self):
        try:
            self.sock.sendall(b"\xC0\x00")
        except Exception as e:
            log("MQTT ping error: {}".format(e))
            self._reconnect()
            return
        # wait for PINGRESP (should be 0xD0 0x00)
        try:
            r, _, _ = select.select([self.sock], [], [], 5)
            if r:
                resp = self.sock.recv(2)
                if not resp:
                    log("MQTT ping: no response (empty)")
                    self._reconnect()
                elif len(resp) < 2:
                    log("MQTT ping: incomplete response (len={})".format(len(resp)))
                    self._reconnect()
                else:
                    first_byte = resp[0] if isinstance(resp[0], int) else ord(resp[0])
                    if first_byte != 0xD0:
                        log("MQTT ping: unexpected response first_byte=0x{:02X}".format(first_byte))
                        self._reconnect()
            else:
                log("MQTT ping: timeout (no data within 5s)")
                self._reconnect()
        except Exception as e:
            log("MQTT ping recv error: {}".format(e))
            self._reconnect()

    def _reconnect(self):
        log("MQTT reconnecting …")
        write_status(mqtt_connected=False)
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        self.sock = None
        while True:
            try:
                self.connect()
                return
            except Exception as e:
                log("MQTT reconnect failed: {} - retry in 15s".format(e))
                time.sleep(15)

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
]

def publish_ha_discovery(mqtt, device_id):
    device = {
        "identifiers": [device_id],
        "name":         "Cube J1 Smart Meter",
        "model":        "Cube J1",
        "manufacturer": "NextDrive",
    }
    base = "cubej/{}".format(device_id)
    for sid, name, unit, dev_class, state_class in SENSOR_DEFS:
        topic  = "homeassistant/sensor/{}/{}/config".format(device_id, sid)
        config = {
            "name":               name,
            "unique_id":          "{}_{}".format(device_id, sid),
            "state_topic":        "{}/{}".format(base, sid),
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
                "fixed_time_reverse_timestamp"):
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

def main():
    global _log_file
    try:
        _log_file = open(LOG_PATH, "a")
    except Exception:
        pass

    write_status(bridge_started_at=now_str(),
                 configuration_required=True,
                 mqtt_connected=False,
                 wisun_connected=False,
                 last_error="Loading configuration")

    cfg           = wait_for_config()
    br_id         = cfg["br_id"]
    br_pwd        = cfg["br_pwd"]
    ha_host       = cfg["mqtt_host"]
    ha_port       = int(cfg.get("mqtt_port", 1883))
    ha_user       = cfg.get("mqtt_user", "")
    ha_pass       = cfg.get("mqtt_pass", "")
    device_id     = cfg.get("device_id", "cubej1")
    serial_port   = cfg.get("serial_port", "/dev/ttyS1")
    poll_interval = int(cfg.get("poll_interval", 60))

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
                 gettable_epcs=[],
                 polling_epcs=epcs_to_hex(DEFAULT_EPCS),
                 last_measurement_at=None,
                 last_values={},
                 last_error="")

    # Connect MQTT
    mqtt = MQTTClient(ha_host, ha_port, "cubej1_{}".format(device_id),
                      username=ha_user, password=ha_pass)
    while True:
        try:
            mqtt.connect()
            break
        except Exception as e:
            log("MQTT connect failed: {} - retry in 15s".format(e))
            write_status(mqtt_connected=False,
                         last_error="MQTT connect failed: {}".format(e))
            time.sleep(15)

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
    try:
        poll_epcs = detect_poll_epcs(fd, ipv6, tid)
        tid = (tid + 1) & 0xFFFF
    except Exception as e:
        log("EPC detection failed: {} - polling default EPCs only".format(e))
        poll_epcs = list(DEFAULT_EPCS)

    while True:
        try:
            orig_led = led_read()
            led_rgb(0, 0, 255)
            try:
                send_el_get(fd, ipv6, tid, poll_epcs)
                tid = (tid + 1) & 0xFFFF
                data = read_erxudp(fd, timeout=15)
                if data:
                    props = parse_el_response(data)
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
                else:
                    log("No ERXUDP response (timeout)")
                    write_status(last_error="No ERXUDP response (timeout)")
            finally:
                led_rgb(*orig_led)

            if time.time() - last_ping > 50:
                mqtt.ping()
                last_ping = time.time()

            if time.time() - last_status_publish > 60:
                publish_bridge_status(mqtt, device_id)
                last_status_publish = time.time()

            time.sleep(poll_interval)

        except Exception as e:
            log("Main loop error: {} - reconnecting Wi-SUN in 30s".format(e))
            write_status(wisun_connected=False,
                         last_error="Main loop error: {}".format(e))
            try:
                publish_bridge_status(mqtt, device_id)
                last_status_publish = time.time()
            except Exception as e_pub:
                log("Status publish failed: {}".format(e_pub))
            time.sleep(30)
            try:
                ipv6 = wisun_connect(fd, br_id, br_pwd)
                log("Wi-SUN reconnected at {}".format(ipv6))
                write_status(wisun_connected=True,
                             meter_ipv6=ipv6,
                             last_error="")
                try:
                    poll_epcs = detect_poll_epcs(fd, ipv6, tid)
                    tid = (tid + 1) & 0xFFFF
                except Exception as e3:
                    log("EPC detection after reconnect failed: {} - keep previous polling EPCs".format(e3))
                    write_status(last_error="EPC detection after reconnect failed: {}".format(e3))
            except Exception as e2:
                log("Wi-SUN reconnect failed: {}".format(e2))
                write_status(wisun_connected=False,
                             last_error="Wi-SUN reconnect failed: {}".format(e2))


if __name__ == "__main__":
    main()
