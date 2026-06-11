#!/usr/bin/python
# -*- coding: utf-8 -*-
"""
config_server.py - tiny web UI for /data/local/config.json
Python 2.7 stdlib only.
"""

from __future__ import print_function

import base64
import collections
import json
import os
import tempfile
import time

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

DEFAULTS = collections.OrderedDict([
    ("br_id", "00000000000000000000000000000000"),
    ("br_pwd", "0123456789AB"),
    ("mqtt_host", "192.168.1.254"),
    ("mqtt_port", 1883),
    ("mqtt_user", "user"),
    ("mqtt_pass", "passwd"),
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
            f.write("[{}] {}\n".format(time.strftime("%Y-%m-%d %H:%M:%S"), msg))
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


def html_escape(s):
    return _html_escape("" if s is None else str(s), quote=True)


def restart_bridge():
    rc = os.system("stop mqtt_ha_bridge >/dev/null 2>&1; sleep 1; start mqtt_ha_bridge >/dev/null 2>&1")
    log("restart_bridge rc={}".format(rc))


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
        if self.path not in ("/", "/index.html"):
            self._send(404, "Not found\n", "text/plain; charset=utf-8")
            return
        if not self._require_auth():
            return
        self._send(200, self._render_form())

    def do_POST(self):
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
            value = params.get(key, [""])[0]
            if key in INT_FIELDS:
                try:
                    value = int(value)
                except Exception:
                    errors.append("{} must be a number".format(key))
            cfg[key] = value

        if cfg.get("web_port", 0) < 1 or cfg.get("web_port", 0) > 65535:
            errors.append("web_port must be between 1 and 65535")
        if cfg.get("mqtt_port", 0) < 1 or cfg.get("mqtt_port", 0) > 65535:
            errors.append("mqtt_port must be between 1 and 65535")
        if cfg.get("poll_interval", 0) < 1:
            errors.append("poll_interval must be greater than 0")
        if not str(cfg.get("web_user", "")):
            errors.append("web_user must not be empty")

        if errors:
            self._send(400, self._render_form(errors=errors, message="Save failed"))
            return

        try:
            save_config(cfg)
            self.server.config = cfg
            if params.get("restart_bridge", [""])[0] == "1":
                restart_bridge()
            self._send(200, self._render_form(message="Saved"))
        except Exception as e:
            log("save failed: {}".format(e))
            self._send(500, self._render_form(errors=[str(e)], message="Save failed"))

    def _render_form(self, message=None, errors=None):
        cfg = load_config()
        status_html = self._render_status(load_status())
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
.actions {{ display: flex; gap: 16px; align-items: center; margin-top: 18px; flex-wrap: wrap; }}
.actions label {{ display: flex; gap: 8px; align-items: center; margin: 0; }}
.actions input[type=checkbox] {{ width: auto; }}
button {{ font-size: 16px; padding: 9px 18px; border: 1px solid #2f6fed; background: #2f6fed; color: #fff; }}
.message {{ background: #e6f4ea; border: 1px solid #9ad0a6; padding: 10px; margin-bottom: 12px; }}
.error {{ background: #fce8e6; border: 1px solid #f2a39b; padding: 10px; margin-bottom: 12px; }}
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
<form method="post" action="/save">
{rows}
<div class="actions">
<button type="submit">Save</button>
<label><input type="checkbox" name="restart_bridge" value="1" checked> Restart MQTT bridge</label>
</div>
</form>
<p>Changing the web port takes effect after reboot or service restart.</p>
</main>
</body>
</html>
""".format(message=message_html, errors=error_html, status=status_html, rows="\n".join(rows))

    def _status_value(self, status, key, default="-"):
        value = status.get(key, default)
        if value in (None, ""):
            return default
        return html_escape(value)

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

        return """<section class="panel">
<h2>Status</h2>
<div class="grid">
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
</section>""".format(
            mqtt=self._bool_status(status.get("mqtt_connected")),
            wisun=self._bool_status(status.get("wisun_connected")),
            device_id=self._status_value(status, "device_id"),
            meter_ipv6=self._status_value(status, "meter_ipv6"),
            started=self._status_value(status, "bridge_started_at"),
            last_measurement=self._status_value(status, "last_measurement_at"),
            updated=self._status_value(status, "updated_at"),
            error_class=error_class,
            last_error=html_escape(last_error),
            values="\n".join(value_rows),
            polling=html_escape(polling),
            gettable=html_escape(gettable))


def main():
    cfg = load_config()
    port = int(cfg.get("web_port", 8080))
    httpd = HTTPServer(("0.0.0.0", port), ConfigHandler)
    httpd.config = cfg
    log("config server start port={}".format(port))
    httpd.serve_forever()


if __name__ == "__main__":
    main()
