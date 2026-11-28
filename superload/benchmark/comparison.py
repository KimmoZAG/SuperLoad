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
from itertools import product
from pathlib import Path

from ..dataset.corpus import generate, open_corpus, page_bytes, read_manifest
from ..dataset.prefix import GEOMETRY, LARGE_MIN, SMALL_MAX
from ..hardware.device import Ascend, CPU, stage_buffer
from ..schemes.bidaw import disk_hrrn
from ..schemes.libra import PipelinedSource
from ..schemes.local import LocalSource
from ..schemes.superload import ReloadPipeline, pipeline_config
from .prefix import CORPUS_SEED, STAGGER_MS


METHODS = ("fifo", "bidaw", "strata-io", "libra", "chunk-rr")
ALL_METHODS = (*METHODS, "superload")
GAPS_MS = (0, STAGGER_MS)
LAYOUTS = ("layer-major", "page-first")
HOST_BUFFERS = ("pageable", "pinned")


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
    host = getattr(args, "host_buffer", "pageable")
    if host != "pageable" and (args.io != "buffered" or args.device == "cpu"):
        raise ValueError("pinned host buffers require Buffered I/O and an Ascend device")
    host_buffers = HOST_BUFFERS if host == "paired" else (host,)
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
        files = {}
        for layout in LAYOUTS:
            fd = open_corpus(corpus, layout, GEOMETRY, args.io, page_count=page_count)
            stack.callback(os.close, fd)
            files[layout] = fd
        candidates = {}
        controls = {}
        native_layout, strata_layout = LAYOUTS
        executor = stack.enter_context(ThreadPoolExecutor(max_workers=1))
        for host_buffer in host_buffers:
            pinned = host_buffer == "pinned"
            stager = stage_buffer(stack, device, length, pinned=pinned)
            native, strata = [LocalSource(files[layout], stager, device, GEOMETRY, layout, page_count)
                              for layout in LAYOUTS]
            buffers = [
                LocalSource(files[native_layout], stage_buffer(stack, device, chunk_size, pinned=pinned),
                            device, GEOMETRY, native_layout, page_count)
                for _ in range(2)
            ]
            libra = PipelinedSource(executor, buffers, args.chunk_blocks, GEOMETRY.page_bytes)
            controls[host_buffer] = {name: {"strata-io": strata, "libra": libra}.get(name, native)
                                     for name in METHODS}
            config = pipeline_config(args.io, host_buffer, cpu=args.device == "cpu")
            candidates[host_buffer] = ReloadPipeline(stack, device, files[strata_layout], page_count,
                                                     config, getattr(args, "numa_node", None))
        memory = {name: candidate.memory() for name, candidate in candidates.items()}
        if args.io == "buffered":
            warm_blocks = 4
            scratch = stage_buffer(stack, device, warm_blocks * GEOMETRY.page_bytes)
            warmers = [LocalSource(files[layout], scratch, device, GEOMETRY, layout, page_count)
                       for layout in LAYOUTS]

        for trial in range(args.repeat):
            order = selected.copy()
            rng.shuffle(order)
            records = []
            for pair_id in order:
                pair = pairs[pair_id]
                if args.io == "buffered":
                    pages = sorted(pair["small"] + pair["large"])
                    for source in warmers:
                        for offset in range(0, len(pages), warm_blocks):
                            source.read(pages[offset:offset + warm_blocks])
                conditions = list(product(GAPS_MS, ALL_METHODS, host_buffers))
                rng.shuffle(conditions)
                expected = {}
                for gap, method, host_buffer in conditions:
                    if method == "superload":
                        row, jobs = candidates[host_buffer].run(pair, gap, base)
                    else:
                        row, jobs = execute(pair, gap, method, controls[host_buffer][method], base, args.chunk_blocks)
                    verified = verify(jobs, device, expected)
                    records.append({"pair": pair_id, "gap_ms": gap, "method": method,
                                    "host_buffer": host_buffer, "verified_blocks": verified, **row})
            trials.append(records)
            print(f"{args.io}: completed trial {trial + 1}/{args.repeat}, {len(records)} controls",
                  file=sys.stderr, flush=True)
    return {
        "schema": 3, "profile": "tool-agent-comparison",
        "plan_sha256": hashlib.sha256(payload).hexdigest(),
        "host_buffers": list(host_buffers), "superload_memory": memory,
        "backend": "cpu" if args.device == "cpu" else "ascend", "io": args.io, "storage": args.storage_label,
        "run_seed": args.seed, "chunk_blocks": args.chunk_blocks, "pairs": selected, "trials": trials,
    }
