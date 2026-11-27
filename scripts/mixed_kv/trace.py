"""Generate the shared deterministic open-loop arrival trace."""

import random
from dataclasses import dataclass, field

from superload.dataset.workloads import make_trace


GIB = 1 << 30
MIB = 1 << 20
PREFIX = "interactive-prefix"
SPARSE = "sparse-decode"
DEADLINE_MS = {PREFIX: 500, SPARSE: 150}
PAGES_PER_PROFILE = 192


@dataclass
class Request:
    number: int
    profile: str
    session: int
    pages: tuple
    selected: tuple
    arrival: int
    deadline: int
    answer_tokens: int
    locations: dict = field(default_factory=dict)
    first: int | None = None
    finished: int | None = None
    prefetched_pages: int = 0
    late_prefetch_pages: int = 0


def arrival_trace(scenario, seed, page_size, target_gib_s, scale):
    rng = random.Random(seed + 10000)
    heavy = scenario == "mix-sparse-heavy"
    waves = {
        PREFIX: make_trace(PREFIX, seed, max(1, (3 if heavy else 6) * scale)),
        SPARSE: make_trace(SPARSE, seed, max(1, (24 if heavy else 18) * scale)),
    }
    all_bytes = sum(len(r.pages) * page_size for w in waves.values() for b in w for r in b.requests)
    span = all_bytes / GIB / target_gib_s
    if scenario.startswith("prefix"):
        profiles = (PREFIX,)
    elif scenario.startswith("sparse"):
        profiles = (SPARSE,)
    else:
        profiles = (PREFIX, SPARSE)
    trace = []
    for k in (PREFIX, SPARSE):
        batches = waves[k]
        slots = []
        for i, batch in enumerate(batches):
            fraction = (i + (0.45 if k == SPARSE else 0.0) + rng.uniform(0, 0.2)) / len(batches)
            slots.append((fraction, batch))
        if k not in profiles:
            continue
        profile_span = span
        if scenario.endswith("-matched"):
            profile_span = (
                sum(len(r.pages) * page_size for b in batches for r in b.requests)
                / GIB
                / target_gib_s
            )
        for fraction, batch in slots:
            trace.extend(
                (int(fraction * profile_span * 1e9), k, request) for request in batch.requests
            )
    shift = min(a for a, _, _ in trace)
    prepared = []
    for n, (relative_ns, kind, r) in enumerate(sorted(trace, key=lambda event: event[0])):
        offset = PAGES_PER_PROFILE if kind == SPARSE else 0
        at = relative_ns - shift
        prepared.append(
            Request(
                n,
                kind,
                r.session,
                tuple(p + offset for p in r.pages),
                tuple(p + offset for p in r.selected),
                at,
                at + DEADLINE_MS[kind] * 1_000_000,
                r.answer_tokens,
            )
        )
    return prepared
