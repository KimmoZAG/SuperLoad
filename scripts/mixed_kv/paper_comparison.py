"""Matched mixed-replay comparison of the three SuperLoad paper mechanisms."""
import argparse
import os
import random
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path

from superload.benchmark.metrics import percentile
from superload.cli import write_json
from superload.dataset.corpus import interleave, open_corpus, page_bytes, read_manifest
from superload.hardware.device import Ascend, CPU, HostBuffer
from superload.schemes.bidaw import HostCache
from superload.schemes.local import LocalSource

from .trace import DEADLINE_MS, GIB, MIB, PREFIX, SPARSE, arrival_trace


SCHEMES = ("native", "edf", "strata", "strata-io", "bidaw", "native-lru", "solid", "solid-io")
LAYOUT = {
    "strata": "page-first",
    "strata-io": "page-first",
    "solid": "interleaved",
    "solid-io": "interleaved",
}
ARENA_BYTES = 4096 * MIB
STAGING_PAGES = 64
CACHE_PAGES = 24
PREFETCH_PAGES = 6


def prepare_trace(scenario, seed, spec, rate):
    trace = arrival_trace(scenario, seed, spec.page_bytes, rate, 1)
    for req in trace:
        if req.profile == SPARSE:
            req.session += 1000
    return trace


def campaign(suite):
    if suite == "smoke":
        return [("mix-balanced", s) for s in ("strata", "bidaw", "solid")]
    if suite == "direct":
        return [("mix-balanced", s) for s in SCHEMES]
    return (
        [(scenario, s) for scenario in ("mix-balanced", "mix-sparse-heavy") for s in SCHEMES]
        + [("prefix-only", s) for s in ("native", "strata", "bidaw", "native-lru")]
        + [("sparse-only", s) for s in ("native", "solid", "solid-io")]
    )


class Trial:
    def __init__(self, args, spec, seed, scheme, scenario, device, arena, staging, pool, trace):
        self.args, self.spec, self.seed = args, spec, seed
        self.scheme, self.scenario, self.device, self.arena, self.trace = (
            scheme,
            scenario,
            device,
            arena,
            trace,
        )
        self.layout = LAYOUT.get(scheme, "layer-major")
        self.staging, self.pool = staging, pool
        self.pending = []
        self.cursor = 0
        self.written = 0
        self.counts = Counter()
        self.submissions = []
        self.cache = (
            HostCache(CACHE_PAGES, scheme == "bidaw") if scheme in ("bidaw", "native-lru") else None
        )
        self.sparse_prefetch = None
        self.prefix_groups = {r.arrival: [] for r in self.trace if r.profile == PREFIX}
        for r in self.trace:
            if r.profile == PREFIX:
                self.prefix_groups[r.arrival].append(r)

    def now(self):
        return time.perf_counter_ns() - self.start

    def admit(self, now):
        while self.cursor < len(self.trace) and self.trace[self.cursor].arrival <= now:
            self.pending.append(self.trace[self.cursor])
            self.cursor += 1

    def choose(self, now):
        if self.scheme == "edf":
            return min(self.pending, key=lambda r: (r.deadline, r.number))
        if self.scheme == "bidaw":
            oldest = next((r.arrival for r in self.pending if r.profile == PREFIX), None)
            eligible = (r for r in self.pending if r.profile != PREFIX or r.arrival == oldest)

            def priority(r):
                misses = sum(p not in self.cache.pages for p in r.pages)
                score = (now - r.arrival) / misses if misses else 0
                return misses > 0, -score, r.number

            return min(eligible, key=priority)
        return self.pending[0]

    def wait_prefetch(self):
        _, future = self.sparse_prefetch
        self.sparse_prefetch = None
        start = self.now()
        values, stats = future.result()
        finished = self.now()
        self.counts.update(stats)
        self.counts["prefetch_wait_ns"] += finished - start
        return values

    def serve(self, loader, request, begin):
        cohort = [request]
        if self.scheme == "strata" and request.profile == PREFIX:
            cohort = [
                r for r in self.pending if r.profile == PREFIX and r.arrival == request.arrival
            ]
        for r in cohort:
            self.pending.remove(r)
            r.first = begin
        pages = (
            tuple(sorted({p for r in cohort for p in r.pages}))
            if len(cohort) > 1
            else request.pages
        )
        if (
            len(pages) > STAGING_PAGES
            or self.written + len(pages) * self.spec.page_bytes > ARENA_BYTES
        ):
            raise RuntimeError("trial exhausted an allocated transfer buffer")
        dest = self.arena + self.written
        prefetched = None
        if self.scheme == "solid" and request.profile == SPARSE and self.sparse_prefetch:
            session, future = self.sparse_prefetch
            if session == request.session:
                prefetched = self.wait_prefetch()
            elif future.done():
                _, stats = future.result()
                self.counts.update(stats)
                self.sparse_prefetch = None
        if self.scheme == "bidaw" and request.profile == PREFIX:
            self.cache.begin_request(request)
        stats = loader.load(pages, dest, cache=self.cache, request=request, prefetched=prefetched)
        done = self.now()
        self.counts.update(stats)
        offsets = {p: self.written + i * self.spec.page_bytes for i, p in enumerate(pages)}
        self.written += len(pages) * self.spec.page_bytes
        for r in cohort:
            r.locations.update({p: offsets[p] for p in r.pages})
            r.finished = done
        if self.scheme == "bidaw" and request.profile == PREFIX:
            group = self.prefix_groups[request.arrival]
            if all(r.finished is not None for r in group):
                self.cache.finish_wave(group)
        self.counts["shared_transfers_saved"] += sum(len(r.pages) for r in cohort) - len(pages)
        self.submissions.append(
            {
                "begin_ns": begin,
                "end_ns": done,
                "request_ids": [r.number for r in cohort],
                "pages": pages,
                **stats,
            }
        )
        if (
            self.scheme == "solid"
            and request.profile == SPARSE
            and request.selected
            and self.sparse_prefetch is None
            and self.cursor < len(self.trace)
        ):
            self.sparse_prefetch = (
                request.session,
                self.pool.submit(loader.prefetch, request.selected[:PREFETCH_PAGES]),
            )

    def run(self):
        fd = open_corpus(self.args.corpus, self.layout, self.spec, self.args.io)
        try:
            loader = LocalSource(fd, self.staging, self.device, self.spec, self.layout)
            self.start = time.perf_counter_ns()
            while self.cursor < len(self.trace) or self.pending:
                now = self.now()
                self.admit(now)
                if self.pending:
                    self.serve(loader, self.choose(now), self.now())
                elif self.cursor < len(self.trace):
                    time.sleep(max(0, (self.trace[self.cursor].arrival - self.now()) / 1e9))
        finally:
            try:
                if self.sparse_prefetch:
                    _, stats = self.sparse_prefetch[1].result()
                    self.counts.update(stats)
            finally:
                os.close(fd)
        self.wall = self.now()

    def collect(self, all_requests):
        rows = []
        summaries = {}
        for kind in (PREFIX, SPARSE):
            group = [r for r in self.trace if r.profile == kind]
            if not group:
                continue
            for r in group if all_requests else (group[0], group[len(group) // 2], group[-1]):
                for p in r.pages:
                    actual = self.device.download(self.arena + r.locations[p], self.spec.page_bytes)
                    expected = page_bytes(self.seed, p, self.spec.page_bytes)
                    if self.layout == "interleaved":
                        expected = interleave(self.spec, expected)
                    if actual != expected:
                        raise ValueError(f"invalid data for {self.scheme} job {r.number} page {p}")
                    self.counts["verified_pages"] += 1
            delays = [(r.finished - r.arrival) / 1e6 for r in group]
            summaries[kind] = {
                "requests": len(group),
                "p50_ms": percentile(delays, 0.50),
                "p95_ms": percentile(delays, 0.95),
                "p99_ms": percentile(delays, 0.99),
                "queue_p95_ms": percentile([(r.first - r.arrival) / 1e6 for r in group], 0.95),
                "on_time": sum(r.finished <= r.deadline for r in group) / len(group),
            }
        for r in self.trace:
            rows.append(
                {
                    "id": r.number,
                    "profile": r.profile,
                    "session": r.session,
                    "arrival_ns": r.arrival,
                    "deadline_ns": r.deadline,
                    "first_ns": r.first,
                    "ready_ns": r.finished,
                    "pages": r.pages,
                }
            )
        logical_bytes = sum(len(r.pages) for r in self.trace) * self.spec.page_bytes
        return {
            "scheme": self.scheme,
            "scenario": self.scenario,
            "io": self.args.io,
            "layout": self.layout,
            "page_bytes": self.spec.page_bytes,
            "nominal_gib_s": self.args.offered_gib_s,
            "staging_bytes": STAGING_PAGES * self.spec.page_bytes,
            "cache_budget_bytes": CACHE_PAGES * self.spec.page_bytes,
            "arena_bytes": ARENA_BYTES,
            "classes": summaries,
            "requests": rows,
            "submissions": self.submissions,
            "counts": dict(self.counts, wall_ns=self.wall, logical_bytes=logical_bytes),
            "ready_gib_s": logical_bytes / GIB / (self.wall / 1e9),
        }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--suite", choices=("smoke", "buffered", "direct"), default="buffered")
    ap.add_argument("--io", choices=("buffered", "direct"), default="buffered")
    ap.add_argument("--device", default="0")
    ap.add_argument("--seeds", type=int, nargs="+", default=(42, 43, 44))
    ap.add_argument("--offered-gib-s", type=float, default=0.28)
    args = ap.parse_args()
    args.output.mkdir(parents=True)
    spec, corpus_seed = read_manifest(args.corpus)
    with ExitStack() as stack:
        device = stack.enter_context(CPU() if args.device == "cpu" else Ascend(int(args.device)))
        base = stack.enter_context(device.allocate(ARENA_BYTES))
        staging = HostBuffer(STAGING_PAGES * spec.page_bytes)
        stack.callback(staging.close)
        pool = stack.enter_context(ThreadPoolExecutor(max_workers=1))
        for seed in args.seeds:
            cells = campaign(args.suite)
            random.Random(seed).shuffle(cells)
            for scenario, scheme in cells:
                print(f"{args.io} {seed} {scenario} {scheme}", flush=True)
                trace = prepare_trace(scenario, seed, spec, args.offered_gib_s)
                trial = Trial(
                    args, spec, corpus_seed, scheme, scenario, device, base, staging, pool, trace
                )
                trial.run()
                result = trial.collect(args.suite == "smoke")
                result.update(
                    seed=seed,
                    backend="cpu" if args.device == "cpu" else "ascend",
                    corpus_seed=corpus_seed,
                    deadline_ms=DEADLINE_MS,
                )
                write_json(result, args.output / f"{seed}.{scenario}.{scheme}.json")


if __name__ == "__main__":
    main()
