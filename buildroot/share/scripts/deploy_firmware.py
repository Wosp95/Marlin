import argparse
import hashlib
import json
import os
import re
import random
import subprocess
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
PROTOCOL = ROOT / "buildroot" / "share" / "scripts" / "MarlinBinaryProtocol.py"
REMOTE_HELPER = ROOT / "buildroot" / "share" / "scripts" / "deploy_remote.py"
SSH_OPTIONS = ["-o", "StrictHostKeyChecking=accept-new"]


def run(command):
    subprocess.run(command, check=True)


def ha_request(base_url, token, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"Authorization": f"Bearer {token}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/{path}",
        data=data,
        headers=headers,
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, json.loads(response.read() or b"null")


def wait_for_plug(base_url, token, entity, expected, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, state = ha_request(base_url, token, f"states/{entity}")
        if state.get("state") == expected:
            return
        time.sleep(1)
    raise RuntimeError(f"smart plug did not report {expected.upper()} within {timeout}s")


def power_cycle(args):
    token = args.ha_token_file.read_text(encoding="utf-8").strip()
    if not token:
        raise SystemExit(f"Home Assistant token file is empty: {args.ha_token_file}")
    entity = args.plug_entity
    ha_request(args.ha_url, token, "services/switch/turn_off", {"entity_id": entity})
    wait_for_plug(args.ha_url, token, entity, "off", 30)
    time.sleep(10)
    ha_request(args.ha_url, token, "services/switch/turn_on", {"entity_id": entity})
    wait_for_plug(args.ha_url, token, entity, "on", 30)


def verify_after_cycle(args, expected_date):
    remote = f"{args.user}@{args.host}"
    deadline = time.time() + args.verify_timeout
    connection_command = (
        "KEY=$(grep -m1 'key:' ~/.octoprint/config.yaml | sed 's/.*key:[[:space:]]*//'); "
        "curl -sS --max-time 5 -H \"X-Api-Key:${KEY}\" http://127.0.0.1/api/connection"
    )
    while time.time() < deadline:
        result = subprocess.run(
            ["ssh", *SSH_OPTIONS, "-i", str(args.key), remote, connection_command],
            capture_output=True,
            text=True,
            check=False,
        )
        try:
            connection = json.loads(result.stdout)
        except json.JSONDecodeError:
            connection = {}
        if connection.get("current", {}).get("state") == "Operational":
            log_command = "strings ~/.octoprint/logs/octoprint.log | grep 'Firmware info line:' | tail -n 1"
            log_result = subprocess.run(
                ["ssh", *SSH_OPTIONS, "-i", str(args.key), remote, log_command],
                capture_output=True,
                text=True,
                check=False,
            )
            firmware_line = log_result.stdout.strip()
            if expected_date not in firmware_line:
                raise RuntimeError(f"post-cycle firmware mismatch: {firmware_line}")
            print(firmware_line)
            return
        time.sleep(2)
    raise RuntimeError("OctoPrint did not return to Operational after the cold power cycle")


def acquire_lock(lock):
    if lock.exists():
        contents = lock.read_text(encoding="utf-8", errors="replace")
        match = re.search(r"PID=(\d+)", contents)
        if not match:
            raise SystemExit(f"deployment lock exists: {lock}")
        try:
            os.kill(int(match.group(1)), 0)
        except ProcessLookupError:
            lock.unlink()
        except OSError as error:
            if getattr(error, "winerror", None) == 87:
                lock.unlink()
            else:
                raise SystemExit(f"cannot inspect deployment lock PID {match.group(1)}: {error}")
        except PermissionError:
            raise SystemExit(f"deployment lock is owned by PID {match.group(1)}")
        else:
            raise SystemExit(f"deployment lock is owned by PID {match.group(1)}")
    try:
        with lock.open("x", encoding="utf-8") as handle:
            handle.write(f"PID={os.getpid()}\nUTC={datetime.now(timezone.utc).isoformat()}\nPurpose=remote firmware deployment\n")
    except FileExistsError:
        raise SystemExit(f"deployment lock exists: {lock}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("firmware", type=Path)
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--remote-python", default="~/.marlin-xfer-venv/bin/python")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--power-cycle", action="store_true")
    parser.add_argument("--ha-url")
    parser.add_argument("--ha-token-file", type=Path)
    parser.add_argument("--plug-entity", default="switch.3d_printer_plug")
    parser.add_argument("--verify-timeout", type=int, default=120)
    args = parser.parse_args()
    firmware = args.firmware.resolve()
    if firmware.suffix.lower() != ".bin":
        raise SystemExit("firmware must be a .bin file")
    if not firmware.is_file():
        raise SystemExit(f"firmware file does not exist: {firmware}")
    if not args.key.is_file():
        raise SystemExit(f"SSH key does not exist: {args.key}")
    if args.power_cycle and not args.ha_token_file:
        raise SystemExit("--ha-token-file is required with --power-cycle")
    if args.power_cycle and not args.ha_url:
        raise SystemExit("--ha-url is required with --power-cycle")
    if args.power_cycle and not args.ha_token_file.is_file():
        raise SystemExit(f"Home Assistant token file does not exist: {args.ha_token_file}")
    digest = hashlib.sha256(firmware.read_bytes()).hexdigest()
    remote = f"{args.user}@{args.host}"
    target = f"FW-{''.join(random.choices('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789', k=5))}.BIN"
    if args.dry_run:
        print(json.dumps({
            "dry_run": True,
            "firmware": str(firmware),
            "sha256": digest,
            "target": target,
            "power_cycle": args.power_cycle,
        }))
        return
    lock = ROOT / ".octoprint_run.lock"
    try:
        acquire_lock(lock)
        with tempfile.TemporaryDirectory() as staging:
            staging_path = Path(staging)
            helper = staging_path / REMOTE_HELPER.name
            protocol = staging_path / PROTOCOL.name
            helper.write_bytes(REMOTE_HELPER.read_bytes())
            protocol.write_bytes(PROTOCOL.read_bytes())
            remote_firmware = f"/tmp/{firmware.name}"
            run(["scp", *SSH_OPTIONS, "-q", "-i", str(args.key), str(firmware), str(helper), str(protocol), f"{remote}:/tmp/"])
            print(f"SHA256 {digest}")
            cleanup = ["ssh", *SSH_OPTIONS, "-i", str(args.key), remote, "rm", "-f", remote_firmware, f"/tmp/{helper.name}", f"/tmp/{protocol.name}"]
            try:
                command = ["ssh", *SSH_OPTIONS, "-i", str(args.key), remote, args.remote_python, f"/tmp/{helper.name}", "--firmware", remote_firmware, "--target", target]
                run(command)
                if args.power_cycle:
                    power_cycle(args)
                    expected_date = extract_compile_date(firmware)
                    verify_after_cycle(args, expected_date)
            finally:
                subprocess.run(cleanup, check=False)
    finally:
        lock.unlink(missing_ok=True)


def extract_compile_date(firmware):
    match = re.search(rb"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}\s+\d{4}", firmware.read_bytes())
    if not match:
        raise RuntimeError(f"could not find compiler date in firmware: {firmware}")
    return match.group(0).decode("ascii")


if __name__ == "__main__":
    main()