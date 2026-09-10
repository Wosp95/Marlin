import argparse
import hashlib
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
PROTOCOL = ROOT / "buildroot" / "share" / "scripts" / "MarlinBinaryProtocol.py"
REMOTE_HELPER = ROOT / "buildroot" / "share" / "scripts" / "deploy_remote.py"
SSH_OPTIONS = ["-o", "StrictHostKeyChecking=accept-new"]


def run(command):
    subprocess.run(command, check=True)


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
    args = parser.parse_args()
    firmware = args.firmware.resolve()
    if firmware.suffix.lower() != ".bin":
        raise SystemExit("firmware must be a .bin file")
    if not firmware.is_file():
        raise SystemExit(f"firmware file does not exist: {firmware}")
    if not args.key.is_file():
        raise SystemExit(f"SSH key does not exist: {args.key}")
    digest = hashlib.sha256(firmware.read_bytes()).hexdigest()
    remote = f"{args.user}@{args.host}"
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
                command = ["ssh", *SSH_OPTIONS, "-i", str(args.key), remote, args.remote_python, f"/tmp/{helper.name}", "--firmware", remote_firmware]
                if args.dry_run:
                    command.append("--dry-run")
                run(command)
            finally:
                subprocess.run(cleanup, check=False)
    finally:
        lock.unlink(missing_ok=True)


if __name__ == "__main__":
    main()