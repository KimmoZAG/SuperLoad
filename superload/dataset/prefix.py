"""Derive storage reloads from anonymized Tool & Agent prefix hashes."""

import hashlib
import json
import random
from collections import Counter, OrderedDict
from itertools import groupby
from pathlib import Path

from .geometry import Geometry


TRACE_URL = (
    "https://raw.githubusercontent.com/kvcache-ai/Mooncake/main/"
    "FAST25-release/traces/toolagent_trace.jsonl"
)
TRACE_SHA256 = "48a2db1a13d3bc05e6330140c64f604ba366df20d3c9e128b5c35a01c1fa5f71"
GEOMETRY = Geometry(tokens=512)
PRIMARY = (8, 16, 64)
CAPACITIES = ((2, 4, 64), (4, 8, 64), PRIMARY, (16, 32, 64), (32, 48, 64), (8, 16, 32), (8, 16, 80))
ORDER_SEEDS = (0, 1, 2)
SMALL_MAX = 4
LARGE_MIN = 16


def replay(windows, capacities, warmup, seed, audit_sharing=False):
    """Read each timestamp against a common residency snapshot, then commit touches."""
    rng = random.Random(seed)
    tiers = [OrderedDict() for _ in capacities]
    hbm, dram, storage = tiers
    counts = Counter()
    histogram = Counter()
    candidates = []
    for index, (timestamp, requests) in enumerate(windows):
        requests = list(requests)
        rng.shuffle(requests)
        plans = []
        for blocks in requests:
            plans.append(tuple(
                (page, 0 if page in hbm else 1 if page in dram else 2 if page in storage else 3)
                for page in blocks
            ))
        for plan in plans:
            for page, level in plan:
                for tier_index, (tier, capacity) in enumerate(zip(tiers, capacities)):
                    if tier_index <= level:
                        tier[page] = None
                        tier.move_to_end(page)
                        if len(tier) > capacity:
                            tier.popitem(last=False)
        if index < warmup:
            continue

        reloads = [tuple(page for page, level in plan if level == 2) for plan in plans]
        small = [pages for pages in reloads if 1 <= len(pages) <= SMALL_MAX]
        large = [pages for pages in reloads if len(pages) >= LARGE_MIN]
        counts["requests"] += len(plans)
        counts["windows"] += 1
        counts["storage_windows"] += int(any(reloads))
        counts["mixed_windows"] += int(bool(small and large))
        counts["small_requests"] += len(small)
        counts["large_requests"] += len(large)
        histogram.update(map(len, reloads))
        disjoint = [(s, l) for s in small for l in large if set(s).isdisjoint(l)]
        if disjoint:
            candidates.append((timestamp, disjoint))
        if audit_sharing:
            active = [set(pages) for pages in reloads if pages]
            counts["shared_storage_windows"] += int(any(
                not a.isdisjoint(b) for i, a in enumerate(active) for b in active[i + 1:]
            ))
    counts["storage_requests"] = counts["requests"] - histogram[0]
    counts["disjoint_windows"] = len(candidates)
    return dict(counts), dict(sorted(histogram.items())), candidates


def read_trace(trace, warmup):
    payload = Path(trace).read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    if digest != TRACE_SHA256:
        raise ValueError(f"Tool & Agent source SHA-256 mismatch: {digest}")
    rows = sorted(map(json.loads, payload.splitlines()), key=lambda row: row["timestamp"])
    windows = [
        (timestamp, [tuple(row["hash_ids"]) for row in group])
        for timestamp, group in groupby(rows, key=lambda row: row["timestamp"])
    ]
    if not 0 <= warmup < len(windows):
        raise ValueError("warmup must leave at least one measurement window")
    return digest, len(rows), windows


def build_plan(trace, sample, warmup, seed):
    digest, request_count, windows = read_trace(trace, warmup)
    sweep = []
    for gib in CAPACITIES:
        blocks = tuple(value * (1 << 30) // GEOMETRY.page_bytes for value in gib)
        for order in ORDER_SEEDS:
            counts, histogram, candidates = replay(windows, blocks, warmup, order)
            sweep.append({
                "capacity_gib": gib, "order_seed": order, **counts,
                "storage_block_histogram": histogram,
            })
            if gib == PRIMARY and order == ORDER_SEEDS[0]:
                primary = candidates

    rng = random.Random(seed)
    chosen = []
    for timestamp, candidates in sorted(rng.sample(primary, min(sample, len(primary)))):
        small, large = rng.choice(candidates)
        chosen.append((timestamp, small, large))
    # Preserve hash order inside each request. The sampled subset gets its own dense file IDs.
    page_ids = sorted({page for _, small, large in chosen for page in (*small, *large)})
    indices = {page: index for index, page in enumerate(page_ids)}
    return {
        "schema": 1, "trace_sha256": digest, "trace_url": TRACE_URL, "requests": request_count,
        "source_windows": len(windows), "warmup_windows": warmup, "page_bytes": GEOMETRY.page_bytes,
        "small_max_blocks": SMALL_MAX, "large_min_blocks": LARGE_MIN, "sample_seed": seed,
        "primary_gib": PRIMARY, "primary_order_seed": ORDER_SEEDS[0], "sweep": sweep,
        "file_page_hashes": page_ids,
        "pairs": [
            {"timestamp_ms": timestamp, "small": [indices[page] for page in small],
             "large": [indices[page] for page in large]}
            for timestamp, small, large in chosen
        ],
    }


def build_comparison(trace, sample, warmup, seed):
    digest, request_count, windows = read_trace(trace, warmup)
    capacities = tuple(gib * (1 << 30) // GEOMETRY.page_bytes for gib in PRIMARY)
    counts, _, disjoint = replay(windows, capacities, warmup, ORDER_SEEDS[0], audit_sharing=True)
    rng = random.Random(seed)
    sampled = [(t, *rng.choice(pairs)) for t, pairs in sorted(rng.sample(disjoint, min(sample, len(disjoint))))]
    pages = sorted({page for _, small, large in sampled for page in (*small, *large)})
    indices = {page: index for index, page in enumerate(pages)}
    return {
        "schema": 1, "workload": "tool-agent-comparison", "trace_sha256": digest,
        "trace_url": TRACE_URL, "requests": request_count, "source_windows": len(windows),
        "warmup_windows": warmup, "capacity_gib": PRIMARY, "order_seed": ORDER_SEEDS[0],
        "sample_seed": seed, "disjoint_windows": counts["disjoint_windows"],
        "shared_storage_windows": counts["shared_storage_windows"],
        "page_bytes": GEOMETRY.page_bytes, "file_page_hashes": pages,
        "pairs": [
            {"timestamp_ms": t, "small": [indices[p] for p in small],
             "large": [indices[p] for p in large]}
            for t, small, large in sampled
        ],
    }
