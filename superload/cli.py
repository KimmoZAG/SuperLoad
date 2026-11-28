"""Command-line interface for Agent prefix replay and measurement."""

import argparse
import json
import os
from pathlib import Path

from .benchmark.comparison import measure as compare_prefix
from .benchmark.prefix import measure as measure_prefix
from .dataset.prefix import build_comparison, build_plan


def positive(text):
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return value


def write_json(data, output):
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        encoded = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        temporary.write_bytes(encoded.encode("utf-8"))
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def parser():
    cli = argparse.ArgumentParser(prog="python3 -m superload")
    commands = cli.add_subparsers(required=True, dest="command")
    for name, help_text, builder, sample in (
        ("prefix-plan", "plan the contention controls", build_plan, 64),
        ("prefix-comparison-plan", "sample matched Tool & Agent reload pairs", build_comparison, 24),
    ):
        command = commands.add_parser(name, help=help_text)
        command.set_defaults(builder=builder)
        command.add_argument("--trace", required=True)
        command.add_argument("--sample", type=positive, default=sample)
        command.add_argument("--warmup", type=int, default=100)
        command.add_argument("--seed", type=int, default=42)
        command.add_argument("--output", required=True)

    for name, help_text, measure, repeat in (
        ("prefix-bench", "measure the contention controls", measure_prefix, 3),
        ("prefix-compare", "compare six methods on identical prefix reloads", compare_prefix, 2),
    ):
        command = commands.add_parser(name, help=help_text)
        command.set_defaults(measure=measure)
        command.add_argument("--corpus", required=True)
        command.add_argument("--io", choices=("buffered", "direct"), default="buffered")
        command.add_argument("--plan", required=True)
        command.add_argument("--device", default="0", help="Ascend logical device or cpu")
        command.add_argument("--repeat", type=positive, default=repeat)
        command.add_argument("--pair-limit", type=positive, help="use a deterministic subset of the plan")
        command.add_argument("--seed", type=int, default=42)
        command.add_argument("--storage-label", default="unspecified")
        command.add_argument("--output", required=True)
        if measure is compare_prefix:
            command.add_argument("--chunk-blocks", type=positive, default=2)
            command.add_argument(
                "--host-buffer", choices=("pageable", "pinned", "paired"), default="pageable",
                help="select ordinary and/or CANN-pinned staging for Buffered I/O",
            )
            command.add_argument("--numa-node", type=int,
                                 help="NPU-local NUMA node for the optimized pageable Buffered I/O pipeline")
    return cli


def main():
    args = parser().parse_args()
    if hasattr(args, "builder"):
        data = args.builder(args.trace, args.sample, args.warmup, args.seed)
    else:
        payload = Path(args.plan).read_bytes()
        data = args.measure(args, json.loads(payload), payload)
    write_json(data, args.output)
    return 0
