import argparse
import json
import re
import time
import urllib.request
from pathlib import Path

import serial

from MarlinBinaryProtocol import FileTransferProtocol, Protocol


TARGET_FILENAME = "firmware.bin"


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
        port.reset_input_buffer()
        port.write(b"M21\r\n")
        deadline = time.time() + 8
        response = ""
        while time.time() < deadline:
            response += port.read(port.in_waiting or 1).decode("utf-8", "replace")
            if "SD card ok" in response or "SD Card Init Fail" in response:
                break
    if "SD card ok" not in response:
        raise RuntimeError(f"SD card check failed: {response.strip()}")


def check_firmware_file(port_name, baud, filename):
    with serial.Serial(port_name, baudrate=baud, timeout=0.2, write_timeout=1) as port:
        port.reset_input_buffer()
        port.write(b"M20\r\n")
        deadline = time.time() + 8
        response = ""
        while time.time() < deadline:
            response += port.read(port.in_waiting or 1).decode("utf-8", "replace")
            if "End file list" in response or "SD Card Init Fail" in response:
                break
    if filename.lower() not in response.lower():
        raise RuntimeError(f"{filename} was not listed on the SD card: {response.strip()}")


def send_command(port_name, baud, command):
    with serial.Serial(port_name, baudrate=baud, timeout=0.2, write_timeout=1) as port:
        port.write(f"{command}\n".encode("ascii"))


def remove_firmware_files(port_name, baud):
    with serial.Serial(port_name, baudrate=baud, timeout=0.2, write_timeout=1) as port:
        port.write(b"M20 F\n")
        deadline = time.time() + 8
        response = ""
        while time.time() < deadline:
            response += port.read(port.in_waiting or 1).decode("utf-8", "replace")
            if "End file list" in response or "SD Card Init Fail" in response:
                break
        if "End file list" not in response:
            raise RuntimeError(f"SD card listing failed: {response.strip()}")
        filenames = []
        for line in response.splitlines():
            match = re.search(r"(?:^|\s)([^/\s]+\.BIN)(?:\s|$)", line, re.IGNORECASE)
            if match:
                filenames.append(match.group(1))
        for filename in filenames:
            port.reset_input_buffer()
            port.write(f"M30 /{filename}\n".encode("ascii"))
            deadline = time.time() + 3
            delete_response = ""
            while time.time() < deadline:
                delete_response += port.read(port.in_waiting or 1).decode("utf-8", "replace")
                if "File deleted" in delete_response or "not found" in delete_response.lower():
                    break
            if "File deleted" not in delete_response and "not found" not in delete_response.lower():
                raise RuntimeError(f"Firmware file '{filename}' was not removed: {delete_response.strip()}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--firmware", required=True)
    parser.add_argument("--port", default="/dev/ttyUSB0")
    parser.add_argument("--baud", type=int, default=250000)
    parser.add_argument("--octoprint-config", default="/home/kieran/.octoprint/config.yaml")
    parser.add_argument("--log", default="/home/kieran/.octoprint/logs/octoprint.log")
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--target", default=TARGET_FILENAME)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    target_filename = args.target

    firmware = Path(args.firmware)
    if not firmware.is_file():
        raise RuntimeError(f"firmware file does not exist: {firmware}")
    if args.dry_run:
        print(json.dumps({"transfer": "skipped", "target": TARGET_FILENAME, "firmware": str(firmware), "dry_run": True}))
        return 0

    api_key = read_api_key(args.octoprint_config)
    start_time = time.time()
    api_request(api_key, "connection", {"command": "disconnect"})
    protocol = None
    try:
        time.sleep(4)
        check_sd_card(args.port, args.baud)
        remove_firmware_files(args.port, args.baud)
        protocol = Protocol(args.port, args.baud, 512, 0.0, 1000)
        protocol.connect()
        transfer = FileTransferProtocol(protocol)
        if not transfer.copy(str(firmware), target_filename, True, False):
            raise RuntimeError("binary transfer failed")
        protocol.disconnect()
        protocol.shutdown()
        protocol = None
        send_command(args.port, args.baud, "M21")
        if not args.dry_run:
            check_firmware_file(args.port, args.baud, target_filename)
        if not args.dry_run:
            send_command(args.port, args.baud, "M997")
        print(json.dumps({"transfer": "ok", "target": target_filename, "dry_run": args.dry_run}))
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