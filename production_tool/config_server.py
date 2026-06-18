#!/usr/bin/python
# -*- coding: utf-8 -*-
"""
config_server.py - tiny web UI for /data/local/config.json
Python 2.7 stdlib only.
"""

from __future__ import print_function

import base64
import collections
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import zipfile

try:
    from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer
    from urlparse import parse_qs
except ImportError:
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs

try:
    from html import escape as _html_escape
except ImportError:
    from cgi import escape as _html_escape


CONFIG_PATH = "/data/local/config.json"
LOG_PATH = "/data/local/config_server.log"
STATUS_PATH = "/data/local/mqtt_status.json"
OTA_STATUS_PATH = "/data/local/ota_status.json"
OTA_UPLOAD_PATH = "/data/local/cube-j1-mqtt-update.zip"
OTA_STAGING_DIR = "/data/local/ota_staging"
OTA_APPLY_SCRIPT = "/data/local/apply_ota_update.sh"
OTA_VERSION_PATH = "/data/local/cube-j1-mqtt.version"
OTA_LOG_PATH = "/data/local/ota_apply.log"
BRIDGE_LOG_PATH = "/data/local/mqtt_bridge.log"
SERIAL_LOG_PATH = "/data/local/serial.log"
AVAHI_CONF_PATH = "/system/etc/avahi-daemon.conf"
MAX_OTA_PACKAGE_SIZE = 2 * 1024 * 1024
MAX_CONFIG_IMPORT_SIZE = 64 * 1024

OTA_ALLOWED_TARGETS = {
    "/data/local/mqtt_bridge.py": "mqtt_bridge.py",
    "/data/local/config_server.py": "config_server.py",
}

DEFAULTS = collections.OrderedDict([
    ("br_id", ""),
    ("br_pwd", ""),
    ("mqtt_host", ""),
    ("mqtt_port", 1883),
    ("mqtt_user", ""),
    ("mqtt_pass", ""),
    ("device_id", "cubej1"),
    ("serial_port", "/dev/ttyS1"),
    ("poll_interval", 60),
    ("web_port", 8080),
    ("web_user", "admin"),
    ("web_pass", "cubej1"),
])

FIELDS = [
    ("br_id", "B-route ID", "text"),
    ("br_pwd", "B-route Password", "password"),
    ("mqtt_host", "MQTT Host", "text"),
    ("mqtt_port", "MQTT Port", "number"),
    ("mqtt_user", "MQTT User", "text"),
    ("mqtt_pass", "MQTT Password", "password"),
    ("device_id", "Device ID", "text"),
    ("serial_port", "Serial Port", "text"),
    ("poll_interval", "Poll Interval (sec)", "number"),
    ("web_port", "Web Port", "number"),
    ("web_user", "Web User", "text"),
    ("web_pass", "Web Password", "password"),
]

INT_FIELDS = set(["mqtt_port", "poll_interval", "web_port"])


def log(msg):
    try:
        with open(LOG_PATH, "a") as f:
            f.write("[{}] {}\n".format(now_str(), msg))
    except Exception:
        pass


def load_config():
    cfg = collections.OrderedDict(DEFAULTS)
    try:
        with open(CONFIG_PATH) as f:
            loaded = json.load(f, object_pairs_hook=collections.OrderedDict)
        for key, val in loaded.items():
            cfg[key] = val
    except Exception as e:
        log("load_config failed: {}".format(e))
    return cfg


def load_status():
    try:
        with open(STATUS_PATH) as f:
            return json.load(f, object_pairs_hook=collections.OrderedDict)
    except Exception as e:
        return collections.OrderedDict([
            ("status_unavailable", True),
            ("message", "Status is not available yet: {}".format(e)),
        ])


def save_config(cfg):
    directory = os.path.dirname(CONFIG_PATH)
    fd, tmp_path = tempfile.mkstemp(prefix=".config.", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f, indent=4)
            f.write("\n")
        os.chmod(tmp_path, 0o600)
        os.rename(tmp_path, CONFIG_PATH)
    except Exception:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        raise


def validate_config_values(cfg):
    errors = []
    for key in INT_FIELDS:
        try:
            cfg[key] = int(cfg.get(key, 0))
        except Exception:
            errors.append("{} must be a number".format(key))

    if cfg.get("web_port", 0) < 1 or cfg.get("web_port", 0) > 65535:
        errors.append("web_port must be between 1 and 65535")
    elif cfg.get("web_port", 0) == 80:
        errors.append("web_port 80 is already used by the device's built-in nginx; choose another port")
    if cfg.get("mqtt_port", 0) < 1 or cfg.get("mqtt_port", 0) > 65535:
        errors.append("mqtt_port must be between 1 and 65535")
    if cfg.get("poll_interval", 0) < 1:
        errors.append("poll_interval must be greater than 0")
    if not str(cfg.get("web_user", "")):
        errors.append("web_user must not be empty")
    return errors


JST_OFFSET_SECONDS = 9 * 3600

def now_str():
    # The device clock runs in UTC with no timezone configured, so apply a
    # fixed JST (UTC+9) offset here rather than relying on system tzdata.
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() + JST_OFFSET_SECONDS))


def load_ota_status():
    try:
        with open(OTA_STATUS_PATH) as f:
            return json.load(f, object_pairs_hook=collections.OrderedDict)
    except Exception:
        return collections.OrderedDict([
            ("state", "idle"),
            ("message", "No OTA update has been applied yet"),
            ("updated_at", now_str()),
        ])


def write_ota_status(state, message, version=None):
    status = collections.OrderedDict([
        ("state", state),
        ("message", message),
        ("updated_at", now_str()),
    ])
    if version:
        status["version"] = version
    tmp_path = OTA_STATUS_PATH + ".tmp"
    try:
        with open(tmp_path, "w") as f:
            json.dump(status, f, indent=2)
            f.write("\n")
        os.rename(tmp_path, OTA_STATUS_PATH)
    except Exception as e:
        log("ota status write failed: {}".format(e))
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


def load_current_version():
    try:
        with open(OTA_VERSION_PATH) as f:
            return f.read().strip()
    except Exception:
        return ""


def has_ota_backup():
    return all(os.path.isfile(target + ".bak") for target in OTA_ALLOWED_TARGETS)


def get_wifi_ssid():
    # wpa_cli creates a reply socket using the caller's umask; under the
    # service's default (restrictive) umask, wpa_supplicant (running as a
    # different user) can't connect back to deliver the reply and the
    # command times out, so relax the umask just for this call.
    tmp_path = "/data/local/.wifi_status.tmp"
    old_umask = os.umask(0)
    try:
        os.system("wpa_cli -p /data/misc/wifi/sockets -i wlan0 status > {} 2>&1".format(shell_quote(tmp_path)))
    except Exception:
        return ""
    finally:
        os.umask(old_umask)
    try:
        with open(tmp_path) as f:
            output = f.read()
        for line in output.splitlines():
            if line.startswith("ssid="):
                return line[len("ssid="):].strip()
    except Exception:
        pass
    return ""


def tail_log_file(path, max_bytes=8192):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            data = f.read()
        if not isinstance(data, str):
            data = data.decode("utf-8", "replace")
        return data
    except Exception:
        return ""


def load_ota_log(max_bytes=8192):
    return tail_log_file(OTA_LOG_PATH, max_bytes)


def load_bridge_log(max_bytes=8192):
    return tail_log_file(BRIDGE_LOG_PATH, max_bytes)


def load_serial_log(max_bytes=8192):
    return tail_log_file(SERIAL_LOG_PATH, max_bytes)


def is_safe_zip_name(name):
    if not name or name.startswith("/") or "\\" in name or ":" in name:
        return False
    norm = os.path.normpath(name)
    if norm == "." or norm.startswith("../") or norm == "..":
        return False
    return norm == name


def validate_version(version):
    if not version:
        return False
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-+"
    return all(ch in allowed for ch in str(version))


def json_loads_zip(data):
    if not isinstance(data, str):
        data = data.decode("utf-8")
    return json.loads(data, object_pairs_hook=collections.OrderedDict)


def validate_ota_package(package_path):
    with zipfile.ZipFile(package_path, "r") as zf:
        names = zf.namelist()
        for name in names:
            if not is_safe_zip_name(name):
                raise ValueError("Unsafe path in update package: {}".format(name))
        if "manifest.json" not in names:
            raise ValueError("manifest.json is missing")

        manifest = json_loads_zip(zf.read("manifest.json"))
        if manifest.get("name") != "cube-j1-mqtt":
            raise ValueError("Unsupported package name")
        if int(manifest.get("format", 0)) != 1:
            raise ValueError("Unsupported package format")
        if manifest.get("device") not in (None, "cube-j1"):
            raise ValueError("Unsupported device")
        if manifest.get("min_installer_format") not in (None, 1):
            raise ValueError("Installer is too old for this package")
        version = str(manifest.get("version", ""))
        if not validate_version(version):
            raise ValueError("Invalid package version")

        files = manifest.get("files")
        if not isinstance(files, list) or not files:
            raise ValueError("manifest files must not be empty")

        seen_targets = set()
        for item in files:
            rel_path = str(item.get("path", ""))
            install_to = str(item.get("install_to", ""))
            expected_sha = str(item.get("sha256", "")).lower()
            mode = str(item.get("mode", "755"))

            if install_to not in OTA_ALLOWED_TARGETS:
                raise ValueError("Install target is not allowed: {}".format(install_to))
            if OTA_ALLOWED_TARGETS[install_to] != rel_path:
                raise ValueError("Path does not match install target: {}".format(rel_path))
            if rel_path not in names:
                raise ValueError("Package file is missing: {}".format(rel_path))
            if mode not in ("644", "755"):
                raise ValueError("Unsupported file mode for {}: {}".format(rel_path, mode))
            data = zf.read(rel_path)
            actual_sha = hashlib.sha256(data).hexdigest()
            if actual_sha != expected_sha:
                raise ValueError("SHA-256 mismatch for {}".format(rel_path))
            seen_targets.add(install_to)

        return manifest


def stage_ota_package(package_path, manifest):
    if os.path.isdir(OTA_STAGING_DIR):
        shutil.rmtree(OTA_STAGING_DIR)
    os.makedirs(OTA_STAGING_DIR)

    with zipfile.ZipFile(package_path, "r") as zf:
        for item in manifest.get("files", []):
            rel_path = str(item.get("path"))
            target = os.path.join(OTA_STAGING_DIR, rel_path)
            with open(target, "wb") as f:
                f.write(zf.read(rel_path))
            os.chmod(target, int(str(item.get("mode", "755")), 8))

        manifest_path = os.path.join(OTA_STAGING_DIR, "manifest.json")
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)
            f.write("\n")


def shell_quote(s):
    return "'" + str(s).replace("'", "'\"'\"'") + "'"


def make_status_json(state, message, version):
    status = collections.OrderedDict([
        ("state", state),
        ("message", message),
        ("updated_at", now_str()),
    ])
    if version:
        status["version"] = version
    return json.dumps(status)


def shell_status_heredoc(state, message, version):
    return [
        "cat > $STATUS <<'JSON'",
        make_status_json(state, message, version),
        "JSON",
    ]


def start_ota_apply(manifest):
    version = str(manifest.get("version", ""))
    lines = [
        "#!/system/bin/sh",
        "LOG={}".format(shell_quote(OTA_LOG_PATH)),
        "STATUS={}".format(shell_quote(OTA_STATUS_PATH)),
        "RESTORE_LIST=\"\"",
        "restore_backups() {",
        "  for TARGET in $RESTORE_LIST; do",
        "    if [ -f \"$TARGET.bak\" ]; then",
        "      cp \"$TARGET.bak\" \"$TARGET\"",
        "      chmod 755 \"$TARGET\"",
        "    fi",
        "  done",
        "}",
        "fail() {",
        "  MSG=\"$1\"",
        "  echo \"[$(TZ=JST-9 date '+%Y-%m-%d %H:%M:%S')] apply failed: $MSG\" >> $LOG",
        "  restore_backups",
        "  cat > $STATUS <<JSON",
        "{\"state\":\"rolled_back\",\"message\":\"OTA apply failed and rollback was attempted: $MSG\",\"updated_at\":\"$(TZ=JST-9 date '+%Y-%m-%d %H:%M:%S')\",\"version\":\"" + version + "\"}",
        "JSON",
        "  start mqtt_ha_bridge >/dev/null 2>&1",
        "  exit 1",
        "}",
        "echo \"[$(TZ=JST-9 date '+%Y-%m-%d %H:%M:%S')] apply start version={}\" >> $LOG".format(version),
    ]
    lines.extend(shell_status_heredoc("applying", "Applying OTA update", version))
    lines.extend([
        "stop mqtt_ha_bridge >/dev/null 2>&1",
        "sleep 1",
    ])

    for item in manifest.get("files", []):
        rel_path = str(item.get("path"))
        install_to = str(item.get("install_to"))
        mode = str(item.get("mode", "755"))
        staged = os.path.join(OTA_STAGING_DIR, rel_path)
        tmp_target = install_to + ".ota"
        lines.extend([
            "cp {} {} || fail {}".format(shell_quote(install_to), shell_quote(install_to + ".bak"), shell_quote("backup " + rel_path)),
            "RESTORE_LIST=\"$RESTORE_LIST {}\"".format(install_to),
            "cp {} {} || fail {}".format(shell_quote(staged), shell_quote(tmp_target), shell_quote("copy " + rel_path)),
            "chmod {} {} || fail {}".format(mode, shell_quote(tmp_target), shell_quote("chmod " + rel_path)),
            "mv {} {} || fail {}".format(shell_quote(tmp_target), shell_quote(install_to), shell_quote("install " + rel_path)),
        ])

    lines.extend([
        "cp {} {} 2>/dev/null".format(shell_quote(OTA_VERSION_PATH), shell_quote(OTA_VERSION_PATH + ".bak")),
        "echo {} > {} || fail {}".format(shell_quote(version), shell_quote(OTA_VERSION_PATH), shell_quote("write version")),
        "start mqtt_ha_bridge >/dev/null 2>&1",
    ])
    lines.extend(shell_status_heredoc("success", "OTA update applied; services restarted", version))
    lines.extend([
        "echo \"[$(TZ=JST-9 date '+%Y-%m-%d %H:%M:%S')] apply success version={}\" >> $LOG".format(version),
        "stop cubej_web_ui >/dev/null 2>&1",
        "sleep 1",
        "start cubej_web_ui >/dev/null 2>&1",
    ])

    with open(OTA_APPLY_SCRIPT, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(OTA_APPLY_SCRIPT, 0o755)
    os.system("setsid /system/bin/sh {} >/dev/null 2>&1 &".format(shell_quote(OTA_APPLY_SCRIPT)))


def start_ota_rollback():
    lines = [
        "#!/system/bin/sh",
        "LOG={}".format(shell_quote(OTA_LOG_PATH)),
        "STATUS={}".format(shell_quote(OTA_STATUS_PATH)),
        "fail() {",
        "  MSG=\"$1\"",
        "  echo \"[$(TZ=JST-9 date '+%Y-%m-%d %H:%M:%S')] manual rollback failed: $MSG\" >> $LOG",
        "  cat > $STATUS <<JSON",
        "{\"state\":\"failed\",\"message\":\"Manual rollback failed: $MSG\",\"updated_at\":\"$(TZ=JST-9 date '+%Y-%m-%d %H:%M:%S')\",\"version\":\"" + load_current_version() + "\"}",
        "JSON",
        "  start mqtt_ha_bridge >/dev/null 2>&1",
        "  exit 1",
        "}",
        "echo \"[$(TZ=JST-9 date '+%Y-%m-%d %H:%M:%S')] manual rollback start\" >> $LOG",
        "cat > $STATUS <<'JSON'",
        make_status_json("applying", "Rolling back to previous OTA backup", load_current_version()),
        "JSON",
        "stop mqtt_ha_bridge >/dev/null 2>&1",
        "sleep 1",
    ]
    for install_to in sorted(OTA_ALLOWED_TARGETS.keys()):
        lines.extend([
            "if [ -f {} ]; then".format(shell_quote(install_to + ".bak")),
            "  cp {} {} || fail {}".format(shell_quote(install_to + ".bak"), shell_quote(install_to), shell_quote("restore " + install_to)),
            "  chmod 755 {} || fail {}".format(shell_quote(install_to), shell_quote("chmod " + install_to)),
            "fi",
        ])
    lines.extend([
        "if [ -f {} ]; then cp {} {}; fi".format(
            shell_quote(OTA_VERSION_PATH + ".bak"),
            shell_quote(OTA_VERSION_PATH + ".bak"),
            shell_quote(OTA_VERSION_PATH)),
        "ROLLED_BACK_VERSION=$(cat {} 2>/dev/null)".format(shell_quote(OTA_VERSION_PATH)),
        "start mqtt_ha_bridge >/dev/null 2>&1",
        "cat > $STATUS <<JSON",
        "{\"state\":\"rolled_back\",\"message\":\"Manual rollback applied; services restarted\",\"updated_at\":\"$(TZ=JST-9 date '+%Y-%m-%d %H:%M:%S')\",\"version\":\"$ROLLED_BACK_VERSION\"}",
        "JSON",
        "echo \"[$(TZ=JST-9 date '+%Y-%m-%d %H:%M:%S')] manual rollback success\" >> $LOG",
        "stop cubej_web_ui >/dev/null 2>&1",
        "sleep 1",
        "start cubej_web_ui >/dev/null 2>&1",
    ])
    with open(OTA_APPLY_SCRIPT, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(OTA_APPLY_SCRIPT, 0o755)
    os.system("setsid /system/bin/sh {} >/dev/null 2>&1 &".format(shell_quote(OTA_APPLY_SCRIPT)))


def html_escape(s):
    return _html_escape("" if s is None else str(s), quote=True)


def restart_bridge():
    rc = os.system("stop mqtt_ha_bridge >/dev/null 2>&1; sleep 1; start mqtt_ha_bridge >/dev/null 2>&1")
    log("restart_bridge rc={}".format(rc))


def reboot_device():
    rc = os.system("(sleep 1; reboot) >/dev/null 2>&1 &")
    log("reboot_device rc={}".format(rc))


def sanitize_hostname(value):
    name = re.sub(r"[^A-Za-z0-9-]", "-", str(value or "")).strip("-")
    return name.lower() or "cubej1"


def sync_avahi_hostname(device_id):
    hostname = sanitize_hostname(device_id)
    try:
        with open(AVAHI_CONF_PATH) as f:
            lines = f.readlines()
    except Exception as e:
        log("avahi config read failed: {}".format(e))
        return

    new_line = "host-name={}\n".format(hostname)
    found = False
    changed = False
    for i, line in enumerate(lines):
        if re.match(r"^#?\s*host-name\s*=", line):
            found = True
            if line != new_line:
                lines[i] = new_line
                changed = True
            break
    if not found:
        lines.insert(0, new_line)
        changed = True

    if not changed:
        return

    try:
        os.system("mount -o rw,remount / >/dev/null 2>&1")
        with open(AVAHI_CONF_PATH, "w") as f:
            f.writelines(lines)
        os.chmod(AVAHI_CONF_PATH, 0o644)
        rc = os.system("stop avahi-daemon >/dev/null 2>&1; sleep 1; start avahi-daemon >/dev/null 2>&1")
        log("avahi host-name set to {} rc={}".format(hostname, rc))
    except Exception as e:
        log("avahi config update failed: {}".format(e))


class ConfigHandler(BaseHTTPRequestHandler):
    server_version = "CubeJ1Config/1.0"

    def log_message(self, fmt, *args):
        log("%s - %s" % (self.client_address[0], fmt % args))

    def _send(self, code, body, content_type="text/html; charset=utf-8"):
        if not isinstance(body, bytes):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _unauthorized(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Cube J1 MQTT Config"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Authentication required\n")

    def _authorized(self):
        cfg = self.server.config
        user = str(cfg.get("web_user", "admin"))
        password = str(cfg.get("web_pass", "cubej1"))
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:].strip())
            if not isinstance(decoded, str):
                decoded = decoded.decode("utf-8")
        except Exception:
            return False
        expected = "{}:{}".format(user, password)
        return decoded == expected

    def _require_auth(self):
        if not self._authorized():
            self._unauthorized()
            return False
        return True

    def do_GET(self):
        if self.path == "/status.json":
            if not self._require_auth():
                return
            self._send(200, json.dumps(load_status(), indent=2) + "\n",
                       "application/json; charset=utf-8")
            return
        if self.path == "/ota_status.json":
            if not self._require_auth():
                return
            self._send(200, json.dumps(load_ota_status(), indent=2) + "\n",
                       "application/json; charset=utf-8")
            return
        if self.path == "/config.json":
            if not self._require_auth():
                return
            self._send(200, json.dumps(load_config(), indent=2) + "\n",
                       "application/json; charset=utf-8")
            return
        if self.path == "/mqtt_bridge.log":
            if not self._require_auth():
                return
            self._send(200, tail_log_file(BRIDGE_LOG_PATH, 262144) or "No log yet\n",
                       "text/plain; charset=utf-8")
            return
        if self.path == "/serial.log":
            if not self._require_auth():
                return
            self._send(200, tail_log_file(SERIAL_LOG_PATH, 262144) or "No log yet\n",
                       "text/plain; charset=utf-8")
            return
        if self.path not in ("/", "/index.html"):
            self._send(404, "Not found\n", "text/plain; charset=utf-8")
            return
        if not self._require_auth():
            return
        self._send(200, self._render_form())

    def do_POST(self):
        if self.path == "/ota/upload":
            if not self._require_auth():
                return
            self._handle_ota_upload()
            return
        if self.path == "/ota/rollback":
            if not self._require_auth():
                return
            if not has_ota_backup():
                self._send(400, self._render_form(errors=["No OTA backup is available to roll back to"], message="Rollback failed"))
                return
            start_ota_rollback()
            self._send(200, self._render_form(message="OTA rollback accepted. Services will restart."))
            return
        if self.path == "/config/import":
            if not self._require_auth():
                return
            self._handle_config_import()
            return
        if self.path == "/reboot":
            if not self._require_auth():
                return
            reboot_device()
            self._send(200, self._render_form(message="Reboot requested. The device will restart in a few seconds."))
            return

        if self.path != "/save":
            self._send(404, "Not found\n", "text/plain; charset=utf-8")
            return
        if not self._require_auth():
            return

        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        if not isinstance(raw, str):
            raw = raw.decode("utf-8")
        params = parse_qs(raw, keep_blank_values=True)
        cfg = load_config()

        errors = []
        for key, _, _ in FIELDS:
            cfg[key] = params.get(key, [""])[0]

        errors.extend(validate_config_values(cfg))

        if errors:
            self._send(400, self._render_form(errors=errors, message="Save failed"))
            return

        try:
            save_config(cfg)
            self.server.config = cfg
            sync_avahi_hostname(cfg.get("device_id", ""))
            if params.get("restart_bridge", [""])[0] == "1":
                restart_bridge()
            self._send(200, self._render_form(message="Saved"))
        except Exception as e:
            log("save failed: {}".format(e))
            self._send(500, self._render_form(errors=[str(e)], message="Save failed"))

    def _handle_config_import(self):
        try:
            import cgi
        except ImportError:
            self._send(500, self._render_form(
                errors=["This Python runtime does not provide cgi.FieldStorage"],
                message="Config import failed"))
            return

        ctype = self.headers.get("Content-Type", "")
        if not ctype.startswith("multipart/form-data"):
            self._send(400, self._render_form(
                errors=["Config import must be multipart/form-data"],
                message="Config import failed"))
            return

        length = int(self.headers.get("Content-Length", "0"))
        if length < 1 or length > MAX_CONFIG_IMPORT_SIZE:
            self._send(400, self._render_form(
                errors=["Config import size must be between 1 byte and {} bytes".format(MAX_CONFIG_IMPORT_SIZE)],
                message="Config import failed"))
            return

        try:
            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={
                    "REQUEST_METHOD": "POST",
                    "CONTENT_TYPE": ctype,
                    "CONTENT_LENGTH": str(length),
                })
            if "config" not in form.keys():
                raise ValueError("No config file was uploaded")
            item = form["config"]
            if isinstance(item, list):
                item = item[0]
            raw = item.file.read(MAX_CONFIG_IMPORT_SIZE + 1)
            if len(raw) > MAX_CONFIG_IMPORT_SIZE:
                raise ValueError("Config file is too large")
            if not isinstance(raw, str):
                raw = raw.decode("utf-8")

            loaded = json.loads(raw, object_pairs_hook=collections.OrderedDict)
            cfg = collections.OrderedDict(DEFAULTS)
            for key, val in loaded.items():
                if key in DEFAULTS:
                    cfg[key] = val

            errors = validate_config_values(cfg)
            if errors:
                self._send(400, self._render_form(errors=errors, message="Config import failed"))
                return

            save_config(cfg)
            self.server.config = cfg
            sync_avahi_hostname(cfg.get("device_id", ""))
            restart_bridge()
            self._send(200, self._render_form(message="Config imported"))
        except Exception as e:
            log("config import failed: {}".format(e))
            self._send(400, self._render_form(errors=[str(e)], message="Config import failed"))

    def _handle_ota_upload(self):
        try:
            import cgi
        except ImportError:
            self._send(500, self._render_form(
                errors=["This Python runtime does not provide cgi.FieldStorage"],
                message="OTA upload failed"))
            return

        ctype = self.headers.get("Content-Type", "")
        if not ctype.startswith("multipart/form-data"):
            self._send(400, self._render_form(
                errors=["OTA upload must be multipart/form-data"],
                message="OTA upload failed"))
            return

        length = int(self.headers.get("Content-Length", "0"))
        if length < 1 or length > MAX_OTA_PACKAGE_SIZE:
            self._send(400, self._render_form(
                errors=["OTA package size must be between 1 byte and {} bytes".format(MAX_OTA_PACKAGE_SIZE)],
                message="OTA upload failed"))
            return

        try:
            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={
                    "REQUEST_METHOD": "POST",
                    "CONTENT_TYPE": ctype,
                    "CONTENT_LENGTH": str(length),
                })
            if "package" not in form.keys():
                raise ValueError("No OTA package was uploaded")
            item = form["package"]
            if isinstance(item, list):
                item = item[0]
            if not getattr(item, "file", None):
                raise ValueError("No OTA package file was uploaded")

            directory = os.path.dirname(OTA_UPLOAD_PATH)
            fd, tmp_path = tempfile.mkstemp(prefix=".ota.", suffix=".zip", dir=directory)
            total = 0
            try:
                with os.fdopen(fd, "wb") as f:
                    while True:
                        chunk = item.file.read(65536)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_OTA_PACKAGE_SIZE:
                            raise ValueError("OTA package is too large")
                        f.write(chunk)
                os.rename(tmp_path, OTA_UPLOAD_PATH)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
                raise

            manifest = validate_ota_package(OTA_UPLOAD_PATH)
            stage_ota_package(OTA_UPLOAD_PATH, manifest)
            version = str(manifest.get("version", ""))
            write_ota_status("uploaded", "OTA package uploaded and validated", version)
            start_ota_apply(manifest)
            self._send(200, self._render_form(message="OTA update accepted. Services will restart."))
        except Exception as e:
            log("ota upload failed: {}".format(e))
            write_ota_status("failed", "OTA upload failed: {}".format(e))
            self._send(400, self._render_form(errors=[str(e)], message="OTA upload failed"))

    def _render_form(self, message=None, errors=None):
        cfg = load_config()
        status_html = self._render_status(load_status())
        ota_html = self._render_ota_panel(load_ota_status())
        config_tools_html = self._render_config_tools()
        rows = []
        for key, label, input_type in FIELDS:
            value = html_escape(cfg.get(key, DEFAULTS.get(key, "")))
            rows.append(
                '<label><span>{}</span><input name="{}" type="{}" value="{}"></label>'.format(
                    html_escape(label), html_escape(key), input_type, value))

        error_html = ""
        if errors:
            error_html = '<div class="error">' + "<br>".join([html_escape(e) for e in errors]) + "</div>"
        message_html = '<div class="message">{}</div>'.format(html_escape(message)) if message else ""

        return """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cube J1 MQTT Config</title>
<style>
body {{ font-family: sans-serif; margin: 0; background: #f6f7f9; color: #202124; }}
main {{ max-width: 760px; margin: 0 auto; padding: 24px; }}
h1 {{ font-size: 24px; margin: 0 0 18px; }}
form {{ background: #fff; border: 1px solid #d8dde3; padding: 18px; }}
.panel {{ background: #fff; border: 1px solid #d8dde3; padding: 18px; margin-bottom: 18px; }}
.panel h2 {{ font-size: 18px; margin: 0 0 12px; }}
.panel h3 {{ font-size: 14px; margin: 16px 0 8px; color: #5f6368; }}
.grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px 18px; }}
.item span {{ display: block; color: #5f6368; font-size: 13px; margin-bottom: 3px; }}
.item strong {{ font-size: 16px; overflow-wrap: anywhere; }}
.ok {{ color: #137333; }}
.bad {{ color: #a50e0e; }}
.muted {{ color: #5f6368; }}
.values {{ margin-top: 14px; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px 18px; }}
.code {{ font-family: monospace; font-size: 13px; overflow-wrap: anywhere; }}
label {{ display: grid; grid-template-columns: 210px 1fr; gap: 12px; align-items: center; margin: 10px 0; }}
input {{ font-size: 16px; padding: 8px; border: 1px solid #b9c0c8; }}
input[type=file] {{ border: 0; padding-left: 0; }}
.actions {{ display: flex; gap: 16px; align-items: center; margin-top: 18px; flex-wrap: wrap; }}
.actions label {{ display: flex; gap: 8px; align-items: center; margin: 0; }}
.actions input[type=checkbox] {{ width: auto; }}
button {{ font-size: 16px; padding: 9px 18px; border: 1px solid #2f6fed; background: #2f6fed; color: #fff; }}
.message {{ background: #e6f4ea; border: 1px solid #9ad0a6; padding: 10px; margin-bottom: 12px; }}
.error {{ background: #fce8e6; border: 1px solid #f2a39b; padding: 10px; margin-bottom: 12px; }}
pre {{ background: #202124; color: #f1f3f4; padding: 10px; overflow: auto; font-size: 12px; max-height: 260px; }}
p {{ line-height: 1.5; }}
@media (max-width: 620px) {{ label, .grid, .values {{ grid-template-columns: 1fr; gap: 4px; }} main {{ padding: 16px; }} }}
</style>
</head>
<body>
<main>
<h1>Cube J1 MQTT Config</h1>
{message}
{errors}
{status}
{ota}
<form method="post" action="/save">
{rows}
<div class="actions">
<button type="submit">Save</button>
<label><input type="checkbox" name="restart_bridge" value="1" checked> Restart MQTT bridge</label>
</div>
</form>
<p>Changing the web port takes effect after reboot or service restart. Port 80 is reserved by the device's built-in nginx and cannot be used.</p>
<form method="post" action="/reboot" onsubmit="return confirm('Reboot the device now?');">
<div class="actions">
<button type="submit">Reboot Device</button>
</div>
</form>
{config_tools}
</main>
<script>
(function() {{
    function refreshLog(url, boxId) {{
        fetch(url).then(function(r) {{ return r.text(); }}).then(function(text) {{
            var box = document.getElementById(boxId);
            if (!box) return;
            if (!text || text === "No log yet") {{
                box.innerHTML = '<p class="muted">No log yet.</p>';
                return;
            }}
            var pre = box.firstElementChild;
            if (!pre || pre.tagName !== "PRE") {{
                box.innerHTML = "<pre></pre>";
                pre = box.firstElementChild;
            }}
            pre.textContent = text;
            pre.scrollTop = pre.scrollHeight;
        }}).catch(function() {{}});
    }}
    setInterval(function() {{
        refreshLog("/mqtt_bridge.log", "bridge-log-box");
        refreshLog("/serial.log", "serial-log-box");
    }}, 5000);
}})();
</script>
</body>
</html>
""".format(message=message_html, errors=error_html, status=status_html,
           ota=ota_html, rows="\n".join(rows), config_tools=config_tools_html)

    def _status_value(self, status, key, default="-"):
        value = status.get(key, default)
        if value in (None, ""):
            return default
        return html_escape(value)

    def _render_ota_panel(self, ota_status):
        current_version = load_current_version() or "-"
        state = ota_status.get("state", "idle")
        state_class = "ok" if state == "success" else ("bad" if state in ("failed", "rolled_back") else "muted")
        version = ota_status.get("version") or "-"
        message = ota_status.get("message") or "-"
        updated = ota_status.get("updated_at") or "-"
        ota_log = load_ota_log()
        log_html = '<p class="muted">No OTA log yet.</p>'
        if ota_log:
            log_html = "<pre>{}</pre>".format(html_escape(ota_log))
        rollback_attrs = "" if has_ota_backup() else ' disabled title="No OTA backup is available to roll back to"'

        return """<section class="panel">
<h2>OTA Update</h2>
<div class="grid">
<div class="item"><span>Current version</span><strong>{current_version}</strong></div>
<div class="item"><span>Last package version</span><strong>{version}</strong></div>
<div class="item"><span>State</span><strong class="{state_class}">{state}</strong></div>
<div class="item"><span>Updated</span><strong>{updated}</strong></div>
</div>
<p>{message}</p>
<form method="post" action="/ota/upload" enctype="multipart/form-data">
<label><span>Update package</span><input name="package" type="file" accept=".zip"></label>
<div class="actions">
<button type="submit">Upload OTA</button>
<a href="/ota_status.json">ota_status.json</a>
</div>
</form>
<form method="post" action="/ota/rollback">
<div class="actions">
<button type="submit"{rollback_attrs}>Rollback OTA</button>
</div>
</form>
{log_html}
</section>""".format(
            current_version=html_escape(current_version),
            version=html_escape(version),
            state_class=state_class,
            state=html_escape(state),
            updated=html_escape(updated),
            message=html_escape(message),
            rollback_attrs=rollback_attrs,
            log_html=log_html)

    def _render_config_tools(self):
        return """<section class="panel">
<h2>Config Backup</h2>
<p><a href="/config.json">Download config.json</a></p>
<form method="post" action="/config/import" enctype="multipart/form-data">
<label><span>Import config</span><input name="config" type="file" accept=".json"></label>
<div class="actions">
<button type="submit">Import Config</button>
</div>
</form>
</section>"""

    def _bool_status(self, value):
        if value is True:
            return '<strong class="ok">connected</strong>'
        if value is False:
            return '<strong class="bad">disconnected</strong>'
        return '<strong class="muted">unknown</strong>'

    def _render_status(self, status):
        values = status.get("last_values") or {}
        value_rows = []
        for key in sorted(values.keys()):
            value_rows.append('<div class="item"><span>{}</span><strong>{}</strong></div>'.format(
                html_escape(key), html_escape(values[key])))
        if not value_rows:
            value_rows.append('<div class="item"><span>values</span><strong class="muted">none yet</strong></div>')

        gettable = ", ".join(status.get("gettable_epcs") or [])
        polling = ", ".join(status.get("polling_epcs") or [])
        if not gettable:
            gettable = "-"
        if not polling:
            polling = "-"

        last_error = status.get("last_error") or "-"
        error_class = "bad" if last_error != "-" else "muted"
        config_state = "required" if status.get("configuration_required") else "ready"
        config_class = "bad" if status.get("configuration_required") else "ok"
        missing_config = ", ".join(status.get("missing_config") or [])
        if not missing_config:
            missing_config = "-"

        wifi_ssid = get_wifi_ssid() or "-"

        bridge_log = load_bridge_log()
        bridge_log_html = '<p class="muted">No bridge log yet.</p>'
        if bridge_log:
            bridge_log_html = "<pre>{}</pre>".format(html_escape(bridge_log))

        serial_log = load_serial_log()
        serial_log_html = '<p class="muted">No serial log yet.</p>'
        if serial_log:
            serial_log_html = "<pre>{}</pre>".format(html_escape(serial_log))

        return """<section class="panel">
<h2>Status</h2>
<div class="grid">
<div class="item"><span>Configuration</span><strong class="{config_class}">{config_state}</strong></div>
<div class="item"><span>Missing config</span><strong>{missing_config}</strong></div>
<div class="item"><span>Wi-Fi SSID</span><strong>{wifi_ssid}</strong></div>
<div class="item"><span>MQTT</span>{mqtt}</div>
<div class="item"><span>Wi-SUN</span>{wisun}</div>
<div class="item"><span>Device ID</span><strong>{device_id}</strong></div>
<div class="item"><span>Meter IPv6</span><strong>{meter_ipv6}</strong></div>
<div class="item"><span>Bridge started</span><strong>{started}</strong></div>
<div class="item"><span>Last measurement</span><strong>{last_measurement}</strong></div>
<div class="item"><span>Updated</span><strong>{updated}</strong></div>
<div class="item"><span>Last error</span><strong class="{error_class}">{last_error}</strong></div>
</div>
<div class="values">{values}</div>
<p class="code">Polling EPCs: {polling}</p>
<p class="code">Gettable EPCs: {gettable}</p>
<p><a href="/status.json">status.json</a></p>
<h3>Bridge Log (last 8KB, auto-refreshes)</h3>
<div id="bridge-log-box">{bridge_log_html}</div>
<p><a href="/mqtt_bridge.log">mqtt_bridge.log (full)</a></p>
<h3>Serial Log /dev/ttyS1 (last 8KB, auto-refreshes)</h3>
<div id="serial-log-box">{serial_log_html}</div>
<p><a href="/serial.log">serial.log (full)</a></p>
</section>""".format(
            wifi_ssid=html_escape(wifi_ssid),
            mqtt=self._bool_status(status.get("mqtt_connected")),
            wisun=self._bool_status(status.get("wisun_connected")),
            device_id=self._status_value(status, "device_id"),
            meter_ipv6=self._status_value(status, "meter_ipv6"),
            started=self._status_value(status, "bridge_started_at"),
            last_measurement=self._status_value(status, "last_measurement_at"),
            updated=self._status_value(status, "updated_at"),
            config_class=config_class,
            config_state=config_state,
            missing_config=html_escape(missing_config),
            error_class=error_class,
            last_error=html_escape(last_error),
            values="\n".join(value_rows),
            polling=html_escape(polling),
            gettable=html_escape(gettable),
            bridge_log_html=bridge_log_html,
            serial_log_html=serial_log_html)


def main():
    cfg = load_config()
    port = int(cfg.get("web_port", 8080))
    sync_avahi_hostname(cfg.get("device_id", ""))
    httpd = HTTPServer(("0.0.0.0", port), ConfigHandler)
    httpd.config = cfg
    log("config server start port={}".format(port))
    httpd.serve_forever()


if __name__ == "__main__":
    main()
