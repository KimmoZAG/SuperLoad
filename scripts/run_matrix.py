"""Run both workload profiles on one device without overlapping test processes."""

import argparse
import random
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def roce_storage(args):
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / "storage.log"
        with path.open("w", encoding="utf-8") as log:
            server = subprocess.Popen([
                sys.executable, "-m", "superload", "serve-roce", "--corpus", args.corpus, "--io", args.io,
                "--hca", args.hca, "--gid-index", str(args.gid_index), "--control-port", "0",
            ], stdout=log, stderr=subprocess.STDOUT)
            try:
                marker = "RoCE storage ready; control port "
                deadline = time.monotonic() + 12
                while True:
                    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                    ready = next((line for line in lines if line.startswith(marker)), None)
                    if ready:
                        break
                    if server.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("\n".join(lines) or "RoCE server did not become ready")
                    time.sleep(0.05)
                yield ["--control-port", ready.removeprefix(marker), "--hca", args.hca,
                       "--gid-index", str(args.gid_index)]
                if server.wait(timeout=12) != 0:
                    raise RuntimeError(path.read_text(encoding="utf-8", errors="replace"))
            finally:
                if server.poll() is None:
                    server.terminate()
                    try:
                        server.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-label", required=True)
    parser.add_argument("--storage-label", default="longhorn-ext4")
    parser.add_argument("--device", default="0")
    parser.add_argument("--io", choices=("buffered", "direct"), default="buffered")
    parser.add_argument("--waves", type=int, default=48)
    parser.add_argument("--warmup", type=int, default=4)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--verify", choices=("last", "all"), default="last")
    parser.add_argument("--roce", action="store_true", help="include the same-node RoCE transport")
    parser.add_argument("--only-roce", action="store_true", help="run just the RoCE cells")
    parser.add_argument("--hca", default="mlx5_2")
    parser.add_argument("--gid-index", type=int, default=5)
    args = parser.parse_args()
    schedules = {
        "interactive-prefix": ("native", "strata", "bidaw", "solid", "native-lru", "strata-io"),
        "sparse-decode": ("native", "strata", "bidaw", "solid", "native-lru", "solid-io"),
    }
    cells = [(profile, scheme) for profile, schemes in schedules.items() for scheme in schemes]
    if args.only_roce:
        cells = [(profile, "roce") for profile in schedules]
    elif args.roce:
        cells.extend((profile, "roce") for profile in schedules)
    random.Random(args.seed).shuffle(cells)
    args.output.mkdir(parents=True, exist_ok=True)
    for profile, scheme in cells:
        destination = args.output / f"{profile}.{scheme}.json"
        if destination.exists():
            parser.error(f"choose an unused result directory: {destination}")
    for profile, scheme in cells:
        print(f"{args.run_label}: {profile} / {scheme}", flush=True)
        command = [
            sys.executable, "-m", "superload", "bench", "--corpus", args.corpus,
            "--profile", profile, "--scheme", scheme, "--device", args.device, "--io", args.io,
            "--waves", str(args.waves), "--warmup", str(args.warmup), "--repeat", str(args.repeat),
            "--seed", str(args.seed), "--verify", args.verify, "--run-label", args.run_label,
            "--storage-label", args.storage_label, "--output", str(args.output / f"{profile}.{scheme}.json"),
        ]
        if scheme == "roce":
            with roce_storage(args) as transport:
                subprocess.run(command + transport, check=True)
        else:
            subprocess.run(command, check=True)


if __name__ == "__main__":
    main()
