import argparse
import json
import re
import time
import urllib.request
from pathlib import Path

import serial

from MarlinBinaryProtocol import FileTransferProtocol, Protocol


def api_request(api_key, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"X-Api-Key": api_key}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        "http://127.0.0.1/api/" + path,
        data=data,
        headers=headers,
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        body = response.read()
        return response.status, json.loads(body) if body else None


def read_api_key(config_path):
    config = Path(config_path).read_text(encoding="utf-8")
    match = re.search(r"(?m)^\s*key:\s*(\S+)", config)
    if not match:
        raise RuntimeError("OctoPrint API key was not found")
    return match.group(1)


def check_sd_card(port_name, baud):
    with serial.Serial(port_name, baudrate=baud, timeout=0.2, write_timeout=1) as port:
        port.write(b"M21\n")
        time.sleep(0.5)
        response = port.read(port.in_waiting).decode("utf-8", "replace")
    if "SD card ok" not in response:
        raise RuntimeError(f"SD card check failed: {response.strip()}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--firmware", required=True)
    parser.add_argument("--port", default="/dev/ttyUSB0")
    parser.add_argument("--baud", type=int, default=250000)
    parser.add_argument("--octoprint-config", default="/home/kieran/.octoprint/config.yaml")
    parser.add_argument("--log", default="/home/kieran/.octoprint/logs/octoprint.log")
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    api_key = read_api_key(args.octoprint_config)
    start_time = time.time()
    api_request(api_key, "connection", {"command": "disconnect"})
    time.sleep(2)
    check_sd_card(args.port, args.baud)

    target = "firmware.bin"
    protocol = None
    try:
        protocol = Protocol(args.port, args.baud, 512, 0.0, 1000)
        protocol.connect()
        transfer = FileTransferProtocol(protocol)
        if not transfer.copy(args.firmware, target, True, args.dry_run):
            raise RuntimeError("binary transfer failed")
        protocol.disconnect()
        protocol.send_ascii("M21")
        if not args.dry_run:
            protocol.send_ascii("M997", True)
        print(json.dumps({"transfer": "ok", "target": target, "dry_run": args.dry_run}))
    finally:
        if protocol:
            protocol.shutdown()

    api_request(api_key, "connection", {"command": "connect"})
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        time.sleep(2)
        _, connection = api_request(api_key, "connection")
        if connection and connection.get("current", {}).get("state") == "Operational":
            log = Path(args.log).read_text(encoding="utf-8", errors="replace")
            recent = []
            for line in log.splitlines():
                timestamp = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
                if timestamp and time.mktime(time.strptime(timestamp.group(1), "%Y-%m-%d %H:%M:%S")) >= start_time:
                    recent.append(line)
            firmware_lines = [line for line in recent if "Firmware info line:" in line]
            print(json.dumps({"reconnected": True, "firmware": firmware_lines[-1] if firmware_lines else None}))
            return 0
    raise RuntimeError("OctoPrint did not return to Operational state")


if __name__ == "__main__":
    raise SystemExit(main())