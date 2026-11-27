"""Match identical small reloads across isolation and shared-path controls."""

import hashlib
import os
import random
import time
from contextlib import ExitStack
from pathlib import Path

from ..dataset.corpus import generate, open_corpus, page_bytes, read_manifest
from ..dataset.prefix import GEOMETRY, LARGE_MIN, SMALL_MAX
from ..hardware.device import Ascend, CPU, HostBuffer
from ..schemes.local import LocalSource


LAYOUT = "layer-major"
CORPUS_SEED = 1729
STAGGER_MS = 25
CASES = {
    "small-only": ("small",),
    "small-first": ("small", "large"),
    "large-first": ("large", "small"),
    "large-first-25ms": ("large", "small"),
}


def execute_pair(pair, case, loader, base):
    start = time.perf_counter_ns()
    rows = {}
    for role in CASES[case]:
        arrival = start
        if role == "small" and case == "large-first-25ms":
            arrival += STAGGER_MS * 1_000_000
        while (remaining := arrival - time.perf_counter_ns()) > 0:
            time.sleep(remaining / 1e9)
        launch = time.perf_counter_ns()
        pages = pair[role]
        offset = 0 if role == "small" else len(pair["small"]) * GEOMETRY.page_bytes
        counters = loader.load(pages, base + offset)
        done = time.perf_counter_ns()
        length = len(pages) * GEOMETRY.page_bytes
        if counters["storage_bytes"] != length or counters["hbm_bytes"] != length:
            raise RuntimeError("reload counters disagree with the planned bytes")
        rows[role] = {
            "queue_ms": (launch - arrival) / 1e6, "ready_ms": (done - arrival) / 1e6,
            "service_ms": (done - launch) / 1e6, **counters,
        }
    return {"requests": rows, "elapsed_ms": (done - start) / 1e6}


def verify_pair(pair, device, base):
    verified = 0
    for role, offset in (("small", 0), ("large", len(pair["small"]))):
        pages = pair[role]
        for index in sorted({0, len(pages) - 1}):
            address = base + (offset + index) * GEOMETRY.page_bytes
            data = device.download(address, GEOMETRY.page_bytes)
            if data != page_bytes(CORPUS_SEED, pages[index], GEOMETRY.page_bytes):
                raise RuntimeError(f"HBM payload mismatch for {role} block {pages[index]}")
            verified += 1
    return verified


def measure(args, plan, plan_bytes):
    pairs = plan["pairs"]
    page_count = len(plan["file_page_hashes"])
    if not pairs or plan["schema"] != 1 or plan["page_bytes"] != GEOMETRY.page_bytes:
        raise ValueError("prefix plan must contain pairs using the benchmark KV geometry")
    for pair in pairs:
        small, large = pair["small"], pair["large"]
        pages = small + large
        if (not 1 <= len(small) <= SMALL_MAX or len(large) < LARGE_MIN
                or len(set(pages)) != len(pages) or min(pages) < 0 or max(pages) >= page_count):
            raise ValueError(f"invalid or overlapping reload blocks at {pair['timestamp_ms']} ms")

    corpus = Path(args.corpus)
    if (corpus / "manifest.json").exists():
        if read_manifest(corpus) != (GEOMETRY, CORPUS_SEED):
            raise ValueError("corpus geometry or seed differs from the prefix benchmark")
    else:
        generate(corpus, GEOMETRY, CORPUS_SEED, page_count=page_count, layouts=(LAYOUT,))

    length = max(len(pair["small"]) + len(pair["large"]) for pair in pairs) * GEOMETRY.page_bytes
    trials = []
    rng = random.Random(args.seed)
    selected = list(range(len(pairs)))
    if args.pair_limit is not None:
        selected = sorted(rng.sample(selected, min(args.pair_limit, len(selected))))
    with ExitStack() as stack:
        device = stack.enter_context(CPU() if args.device == "cpu" else Ascend(int(args.device)))
        base = stack.enter_context(device.allocate(length))
        stager = HostBuffer(length)
        stack.callback(stager.close)
        for index in range(0, length, os.sysconf("SC_PAGESIZE")):
            stager.view[index] = 0
        fd = open_corpus(corpus, LAYOUT, GEOMETRY, args.io, page_count=page_count)
        stack.callback(os.close, fd)
        loader = LocalSource(fd, stager, device, GEOMETRY, LAYOUT, page_count)
        execute_pair(pairs[selected[0]], "small-first", loader, base)
        for _ in range(args.repeat):
            order = selected.copy()
            rng.shuffle(order)
            samples = []
            for index in order:
                pair = pairs[index]
                cases = list(CASES)
                rng.shuffle(cases)
                if args.io == "buffered":
                    execute_pair(pair, "small-first", loader, base)
                rows = {}
                for case in cases:
                    rows[case] = execute_pair(pair, case, loader, base)
                    if case == "large-first":
                        verified = verify_pair(pair, device, base)
                samples.append({"pair": index, "cases": rows, "verified_blocks": verified})
            trials.append(samples)
    return {
        "schema": 1, "profile": "tool-agent-prefix", "backend": "cpu" if args.device == "cpu" else "ascend",
        "io": args.io, "layout": LAYOUT, "run_seed": args.seed, "storage": args.storage_label,
        "plan_sha256": hashlib.sha256(plan_bytes).hexdigest(), "stagger_ms": STAGGER_MS,
        "pairs": selected, "trials": trials,
    }
