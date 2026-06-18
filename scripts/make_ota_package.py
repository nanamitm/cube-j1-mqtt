#!/usr/bin/env python3
"""Build a Cube J1 MQTT OTA update package."""

import argparse
import hashlib
import json
import pathlib
import zipfile


PACKAGE_NAME = "cube-j1-mqtt"
PACKAGE_FORMAT = 1
DEVICE = "cube-j1"
MIN_INSTALLER_FORMAT = 1

FILES = [
    {
        "path": "mqtt_bridge.py",
        "source": "production_tool/mqtt_bridge.py",
        "install_to": "/data/local/mqtt_bridge.py",
        "mode": "755",
    },
    {
        "path": "config_server.py",
        "source": "production_tool/config_server.py",
        "install_to": "/data/local/config_server.py",
        "mode": "755",
    },
]


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(root, version):
    files = []
    for item in FILES:
        source = root / item["source"]
        if not source.is_file():
            raise FileNotFoundError(source)
        files.append({
            "path": item["path"],
            "install_to": item["install_to"],
            "mode": item["mode"],
            "sha256": sha256_file(source),
        })

    return {
        "name": PACKAGE_NAME,
        "device": DEVICE,
        "format": PACKAGE_FORMAT,
        "min_installer_format": MIN_INSTALLER_FORMAT,
        "version": version,
        "files": files,
        "restart": [
            "mqtt_ha_bridge",
            "cubej_web_ui",
        ],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--root", default=".")
    args = parser.parse_args()

    root = pathlib.Path(args.root).resolve()
    output = pathlib.Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    manifest = build_manifest(root, args.version)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")
        for item in FILES:
            zf.write(root / item["source"], item["path"])

    print(output)


if __name__ == "__main__":
    main()
