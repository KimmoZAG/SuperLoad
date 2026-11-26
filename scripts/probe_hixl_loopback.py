"""Run the two HIXL processes on one logical device to expose topology limits."""

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--device", default="0")
    ap.add_argument("--engine-port", type=int, default=35481)
    ap.add_argument("--control-port", type=int, default=35482)
    args = ap.parse_args()
    command = [sys.executable, "-m", "superload"]
    shared = ["--corpus", args.corpus, "--device", args.device, "--control-port", str(args.control_port)]
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / "server.log"
        with path.open("w", encoding="utf-8") as out:
            server = subprocess.Popen([*command, "serve-hixl", *shared,
                                       "--engine", f"127.0.0.1:{args.engine_port}"],
                                      stdout=out, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 30
                while "HIXL server ready" not in path.read_text(encoding="utf-8", errors="replace"):
                    if server.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError(path.read_text(encoding="utf-8", errors="replace"))
                    time.sleep(0.1)
                status = subprocess.run([
                    *command, "bench", *shared, "--scheme", "hixl", "--profile", "interactive-prefix",
                    "--waves", "4", "--warmup", "0", "--repeat", "1", "--verify", "all",
                    "--run-label", "same-device", "--storage-label", "longhorn-ext4", "--output", args.output,
                ], timeout=45, check=False)
                if status.returncode not in (0, 2):
                    status.check_returncode()
                if server.wait(timeout=20) != 0:
                    raise RuntimeError(path.read_text(encoding="utf-8", errors="replace"))
                evidence = json.loads(Path(args.output).read_text(encoding="utf-8"))
                print(f"server host registration succeeded; client status={evidence['status']}")
            finally:
                if server.poll() is None:
                    server.terminate()
                    try:
                        server.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait()


if __name__ == "__main__":
    main()
