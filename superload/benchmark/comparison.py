"""Compare request ordering, storage layout, and chunking on identical reloads."""

from __future__ import annotations

import hashlib
import os
import random
import sys
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

from ..dataset.corpus import generate, open_corpus, page_bytes, read_manifest
from ..dataset.prefix import GEOMETRY, LARGE_MIN, SMALL_MAX
from ..hardware.device import Ascend, CPU, HostBuffer
from ..schemes.bidaw import disk_hrrn
from ..schemes.libra import PipelinedSource
from ..schemes.local import LocalSource
from .prefix import CORPUS_SEED, STAGGER_MS


METHODS = ("fifo", "bidaw", "strata-io", "libra", "chunk-rr")
GAPS_MS = (0, STAGGER_MS)
LAYOUTS = ("layer-major", "page-first")


@dataclass
class Reload:
    role: str
    blocks: tuple
    arrival: int
    address: int
    sent: int = 0
    first: int | None = None
    service_ns: int = 0
    counters: Counter = field(default_factory=Counter)


def admit(ready, arrivals, now):
    while arrivals and arrivals[0].arrival <= now:
        ready.append(arrivals.popleft())


def next_reload(ready, method, now):
    if method != "bidaw":
        return ready.popleft()
    selected = disk_hrrn(ready, now)
    ready.remove(selected)
    return selected


def execute(pair, gap, method, source, base, chunk_blocks):
    block_bytes = GEOMETRY.page_bytes
    small = tuple(pair["small"])
    large = tuple(pair["large"])
    if method == "strata-io":
        small, large = tuple(sorted(small)), tuple(sorted(large))
    start = time.perf_counter_ns()
    jobs = (
        Reload("large", large, start, base + len(small) * block_bytes),
        Reload("small", small, start + gap * 1_000_000, base),
    )
    arrivals, ready = deque(jobs), deque()
    rows = {}
    while ready or arrivals:
        now = time.perf_counter_ns()
        if not ready:
            while (remaining := arrivals[0].arrival - now) > 0:
                time.sleep(remaining / 1e9)
                now = time.perf_counter_ns()
        admit(ready, arrivals, now)
        job = next_reload(ready, method, now)
        end = len(job.blocks) if method != "chunk-rr" else min(len(job.blocks), job.sent + chunk_blocks)
        launch = time.perf_counter_ns()
        if job.first is None:
            job.first = launch
        counters = source.load(job.blocks[job.sent:end], job.address + job.sent * block_bytes)
        done = time.perf_counter_ns()
        job.counters.update(counters)
        job.service_ns += done - launch
        job.sent = end
        if end < len(job.blocks):
            admit(ready, arrivals, done)
            ready.append(job)
            continue

        amount = len(job.blocks) * block_bytes
        if job.counters["storage_bytes"] != amount or job.counters["hbm_bytes"] != amount:
            raise RuntimeError(f"{method} moved the wrong number of bytes for {job.role}")
        rows[job.role] = {
            "pages": len(job.blocks), "queue_ms": (job.first - job.arrival) / 1e6,
            "ready_ms": (done - job.arrival) / 1e6, "service_ms": job.service_ns / 1e6,
            **job.counters,
        }
    return {"requests": rows, "elapsed_ms": (done - start) / 1e6}, jobs


def verify(jobs, device, expected):
    count = 0
    for request in jobs:
        for i in sorted({0, len(request.blocks) - 1}):
            page = request.blocks[i]
            if page not in expected:
                expected[page] = page_bytes(CORPUS_SEED, page, GEOMETRY.page_bytes)
            data = device.download(request.address + i * GEOMETRY.page_bytes, GEOMETRY.page_bytes)
            if data != expected[page]:
                raise RuntimeError(f"HBM verification failed for {request.role} block {page}")
            count += 1
    return count


def measure(args, plan, payload):
    pairs = plan["pairs"]
    page_count = len(plan["file_page_hashes"])
    if not pairs or plan["workload"] != "tool-agent-comparison" or plan["page_bytes"] != GEOMETRY.page_bytes:
        raise ValueError("use a prefix-comparison-plan output")
    for pair in pairs:
        small, large = pair["small"], pair["large"]
        pages = small + large
        if (not 1 <= len(small) <= SMALL_MAX or len(large) < LARGE_MIN
                or len(set(pages)) != len(pages) or min(pages) < 0 or max(pages) >= page_count):
            raise ValueError(f"invalid comparison pair at {pair['timestamp_ms']} ms")

    corpus = Path(args.corpus)
    if not (corpus / "manifest.json").exists():
        generate(corpus, GEOMETRY, CORPUS_SEED, page_count=page_count, layouts=LAYOUTS)
    if read_manifest(corpus) != (GEOMETRY, CORPUS_SEED):
        raise ValueError("corpus geometry or seed differs from the comparison plan")
    length = max(len(pair["small"]) + len(pair["large"]) for pair in pairs) * GEOMETRY.page_bytes
    chunk_size = min(length, args.chunk_blocks * GEOMETRY.page_bytes)
    rng = random.Random(args.seed)
    trials = []
    selected = list(range(len(pairs)))
    if args.pair_limit is not None:
        selected = sorted(rng.sample(selected, min(len(selected), args.pair_limit)))
    with ExitStack() as stack:
        device = stack.enter_context(CPU() if args.device == "cpu" else Ascend(int(args.device)))
        base = stack.enter_context(device.allocate(length))
        stager = HostBuffer(length)
        stack.callback(stager.close)
        for i in range(0, length, os.sysconf("SC_PAGESIZE")):
            stager.view[i] = 0
        sources = {}
        for layout in LAYOUTS:
            fd = open_corpus(corpus, layout, GEOMETRY, args.io, page_count=page_count)
            stack.callback(os.close, fd)
            sources[layout] = LocalSource(fd, stager, device, GEOMETRY, layout, page_count)
        native = sources["layer-major"]
        buffers = []
        for _ in range(2):
            buffer = HostBuffer(chunk_size)
            stack.callback(buffer.close)
            for i in range(0, chunk_size, os.sysconf("SC_PAGESIZE")):
                buffer.view[i] = 0
            buffers.append(LocalSource(native.fd, buffer, device, GEOMETRY, "layer-major", page_count))
        executor = stack.enter_context(ThreadPoolExecutor(max_workers=1))
        libra = PipelinedSource(executor, buffers, args.chunk_blocks, GEOMETRY.page_bytes)
        loaders = {"strata-io": sources["page-first"], "libra": libra}

        warm = pairs[selected[0]]["small"]
        for source in (native, sources["page-first"], libra):
            source.load(warm, base)
        for trial in range(args.repeat):
            order = selected.copy()
            rng.shuffle(order)
            records = []
            for pair_id in order:
                pair = pairs[pair_id]
                if args.io == "buffered":
                    for source in sources.values():
                        source.read(pair["small"] + pair["large"])
                conditions = [(gap, method) for gap in GAPS_MS for method in METHODS]
                rng.shuffle(conditions)
                expected = {}
                for gap, method in conditions:
                    row, jobs = execute(pair, gap, method, loaders.get(method, native), base, args.chunk_blocks)
                    verified = verify(jobs, device, expected) if trial == 0 or pair_id == order[0] else 0
                    records.append({"pair": pair_id, "gap_ms": gap, "method": method,
                                    "verified_blocks": verified, **row})
            trials.append(records)
            print(f"{args.io}: completed trial {trial + 1}/{args.repeat}, {len(records)} controls",
                  file=sys.stderr, flush=True)
    return {
        "schema": 1, "profile": "tool-agent-comparison", "plan_sha256": hashlib.sha256(payload).hexdigest(),
        "backend": "cpu" if args.device == "cpu" else "ascend", "io": args.io, "storage": args.storage_label,
        "run_seed": args.seed, "chunk_blocks": args.chunk_blocks, "pairs": selected, "trials": trials,
    }
