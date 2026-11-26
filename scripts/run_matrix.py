"""Run both workload profiles on one device without overlapping test processes."""

import argparse
import random
import subprocess
import sys
from pathlib import Path


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
    args = parser.parse_args()
    schedules = {
        "interactive-prefix": ("native", "strata", "bidaw", "solid", "native-lru", "strata-io"),
        "sparse-decode": ("native", "strata", "bidaw", "solid", "native-lru", "solid-io"),
    }
    cells = [(profile, scheme) for profile, schemes in schedules.items() for scheme in schemes]
    random.Random(args.seed).shuffle(cells)
    args.output.mkdir(parents=True, exist_ok=True)
    for profile, scheme in cells:
        destination = args.output / f"{profile}.{scheme}.json"
        if destination.exists():
            parser.error(f"choose an unused result directory: {destination}")
    for profile, scheme in cells:
        print(f"{args.run_label}: {profile} / {scheme}", flush=True)
        subprocess.run([
            sys.executable, "-m", "superload", "bench", "--corpus", args.corpus,
            "--profile", profile, "--scheme", scheme, "--device", args.device, "--io", args.io,
            "--waves", str(args.waves), "--warmup", str(args.warmup), "--repeat", str(args.repeat),
            "--seed", str(args.seed), "--verify", args.verify, "--run-label", args.run_label,
            "--storage-label", args.storage_label, "--output", str(args.output / f"{profile}.{scheme}.json"),
        ], check=True)


if __name__ == "__main__":
    main()
