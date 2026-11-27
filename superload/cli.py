"""Command-line entry points for building data, replaying traces, and serving RoCE."""

import argparse
import json
import os
from dataclasses import asdict
from pathlib import Path

from .benchmark import CONTROLS, SCHEMES, run
from .benchmark.prefix import measure as measure_prefix
from .dataset.corpus import generate, read_manifest
from .dataset.prefix import build_plan
from .dataset.workloads import Geometry, PAGE_COUNT, PROFILES, make_trace
from .schemes.roce import serve


def positive(text):
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return value


def write_json(data, output=None):
    if not output:
        print(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False))
        return
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def corpus_args(command):
    command.add_argument("--corpus", required=True)
    command.add_argument("--io", choices=("buffered", "direct"), default="buffered")


def roce_args(command):
    command.add_argument("--control-port", type=int, default=35482)
    command.add_argument("--bridge", default="build/native/libsuperload_roce.so")
    command.add_argument("--hca", default="mlx5_2")
    command.add_argument("--ib-port", type=positive, default=1)
    command.add_argument("--gid-index", type=int, default=5)
    command.add_argument("--mr-bytes", type=positive, default=32768)
    command.add_argument("--timeout-ms", type=positive, default=8000)


def parser():
    cli = argparse.ArgumentParser(prog="python3 -m superload")
    commands = cli.add_subparsers(required=True, dest="command")
    build = commands.add_parser("build-corpus", help="generate equivalent layouts of deterministic KV bytes")
    build.add_argument("directory")
    build.add_argument("--seed", type=int, default=1729)
    build.add_argument("--layers", type=positive, default=4)
    build.add_argument("--heads", type=positive, default=8)
    build.add_argument("--head-dim", type=positive, default=128)
    build.add_argument("--tokens", type=positive, default=128)
    build.add_argument("--dtype-bytes", type=positive, default=2)

    trace = commands.add_parser("trace", help="emit the complete request trace as JSON")
    trace.add_argument("--profile", choices=PROFILES, required=True)
    trace.add_argument("--seed", type=int, default=42)
    trace.add_argument("--waves", type=positive, default=64)
    trace.add_argument("--output")

    prefix = commands.add_parser("prefix-plan", help="derive reload cohorts from the Mooncake Tool & Agent trace")
    prefix.add_argument("--trace", required=True)
    prefix.add_argument("--sample", type=positive, default=64)
    prefix.add_argument("--warmup", type=int, default=100)
    prefix.add_argument("--seed", type=int, default=42)
    prefix.add_argument("--output", required=True)

    pairs = commands.add_parser("prefix-bench", help="measure matched storage-to-HBM prefix reloads")
    corpus_args(pairs)
    pairs.add_argument("--plan", required=True)
    pairs.add_argument("--device", default="0")
    pairs.add_argument("--repeat", type=positive, default=3)
    pairs.add_argument("--pair-limit", type=positive, help="measure a deterministic subset of the planned pairs")
    pairs.add_argument("--seed", type=int, default=42)
    pairs.add_argument("--storage-label", default="unspecified")
    pairs.add_argument("--output", required=True)

    bench = commands.add_parser("bench", help="measure a scheme or an ablation against the same trace")
    corpus_args(bench)
    roce_args(bench)
    bench.add_argument("--profile", choices=PROFILES, required=True)
    bench.add_argument("--scheme", choices=SCHEMES + CONTROLS, required=True)
    bench.add_argument("--device", default="0", help="logical Ascend device, or cpu for correctness tests")
    bench.add_argument("--seed", type=int, default=42)
    bench.add_argument("--waves", type=positive, default=64)
    bench.add_argument("--warmup", type=int, default=4)
    bench.add_argument("--repeat", type=positive, default=3)
    bench.add_argument("--cache-pages", type=int, default=24)
    bench.add_argument("--prefetch-pages", type=int, default=6)
    bench.add_argument("--verify", choices=("last", "all"), default="last")
    bench.add_argument("--storage-label", default="unspecified")
    bench.add_argument("--run-label", default="unspecified")
    bench.add_argument("--output")
    bench.add_argument("--control-host", default="127.0.0.1")

    server = commands.add_parser("serve-roce", help="serve a fixed local KV corpus over RoCE RC")
    corpus_args(server)
    roce_args(server)
    server.add_argument("--bind", default="127.0.0.1", help="IPv4 control address")
    server.add_argument("--max-pages", type=positive, default=16)
    server.add_argument("--idle-timeout", type=positive, default=120)
    return cli


def main():
    cli = parser()
    args = cli.parse_args()
    if args.command == "build-corpus":
        spec = Geometry(args.layers, args.heads, args.head_dim, args.tokens, args.dtype_bytes)
        generate(args.directory, spec, args.seed)
        print(f"{PAGE_COUNT} pages, {spec.page_bytes} bytes/page, three layouts")
        return 0
    if args.command == "trace":
        waves = make_trace(args.profile, args.seed, args.waves)
        write_json({"profile": args.profile, "seed": args.seed, "waves": [asdict(w) for w in waves]}, args.output)
        return 0
    if args.command == "prefix-plan":
        write_json(build_plan(args.trace, args.sample, args.warmup, args.seed), args.output)
        return 0
    if args.command == "prefix-bench":
        payload = Path(args.plan).read_bytes()
        write_json(measure_prefix(args, json.loads(payload), payload), args.output)
        return 0
    if args.command == "serve-roce":
        if args.max_pages > PAGE_COUNT:
            cli.error(f"max-pages may not exceed {PAGE_COUNT}")
        serve(args)
        return 0
    if min(args.warmup, args.cache_pages, args.prefetch_pages) < 0:
        cli.error("warmup, cache-pages and prefetch-pages must be nonnegative")
    spec, corpus_seed = read_manifest(args.corpus)
    result = {
        "schema": 1, "scheme": args.scheme, "profile": args.profile,
        "backend": "cpu" if args.device == "cpu" else "ascend",
        "io": args.io, "storage": args.storage_label, "run": args.run_label,
        "geometry": asdict(spec), "corpus_seed": corpus_seed, "trace_seed": args.seed,
        "waves": args.waves, "repeat": args.repeat, "warmup": args.warmup, "verify": args.verify,
        "cache_pages": args.cache_pages, "prefetch_pages": args.prefetch_pages,
    }
    if args.scheme == "roce":
        result["roce"] = {
            "hca": args.hca, "port": args.ib_port, "gid_index": args.gid_index, "mr_bytes": args.mr_bytes,
            "control_loopback": args.control_host == "127.0.0.1",
        }
    result.update(run(args, spec, corpus_seed))
    write_json(result, args.output)
    return 0
