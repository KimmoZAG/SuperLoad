"""Measure and reduce residual blocking with the same Strata prefix page union."""
import argparse
import math
import os
import random
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path

from superload.benchmark.metrics import percentile
from superload.cli import write_json
from superload.dataset.corpus import make_plan, open_corpus, page_bytes, read_manifest, read_spans
from superload.hardware.device import Ascend, CPU, HostBuffer

from .trace import GIB, MIB, PREFIX, SPARSE, arrival_trace


ARENA_MIB = 4096
SLOT_PAGES = 64
SPARSE_SLOT_PAGES = 10
POLICIES = ("strata", "h2d-yield", "read-h2d-yield")
PIPELINE_POLICIES = ("strata", "h2d-yield", "h2d-pipeline")


class Trial:
    def __init__(self, requests, fd, spec, device, base, staging, policy, quantum, reader):
        self.requests = requests
        self.fd, self.spec, self.device, self.base, self.staging = fd, spec, device, base, staging
        self.size = spec.page_bytes
        self.policy, self.quantum = policy, quantum
        self.pending = deque()
        self.cursor = 0
        self.written = 0
        self.events = []
        self.counters = Counter()
        self.task_io = {}
        self.reader = reader
        self.pending_read = None

    def now(self):
        return time.perf_counter_ns() - self.origin

    def admit(self):
        now = self.now()
        while self.cursor < len(self.requests) and self.requests[self.cursor].arrival <= now:
            self.pending.append(self.requests[self.cursor])
            self.cursor += 1

    def read_at(self, pages, offset):
        start = self.now()
        spans = make_plan("page-first", self.spec, pages)
        offset_bytes = offset * self.size
        end_bytes = offset_bytes + len(pages) * self.size
        with self.staging.view[offset_bytes:end_bytes] as view:
            read_start = self.now()
            calls, amount = read_spans(self.fd, view, spans)
            ready = self.now()
        return start, read_start, ready, calls, amount

    def record_read(self, trace, owner):
        start, read_start, ready, calls, amount = trace
        self.events.append((owner, "read", read_start, ready))
        self.counters.update(read_calls=calls, storage_bytes=amount, read_ns=ready - read_start)
        return {"read_ns": ready - read_start, "plan_ns": read_start - start}

    def stage(self, pages, offset, owner):
        return self.record_read(self.read_at(pages, offset), owner)

    def copy(self, offset, first, count, owner):
        size = count * self.size
        source = self.staging.address + offset * self.size
        destination = self.base + first * self.size
        start = self.now()
        self.device.upload(destination, source, size)
        done = self.now()
        self.events.append((owner, "h2d", start, done))
        self.counters.update(hbm_bytes=size, h2d_ns=done - start, copies=1)
        return done, done - start

    def reserve(self, pages):
        first = self.written
        self.written += len(pages)
        if self.written * self.size > ARENA_MIB * MIB:
            raise RuntimeError("fixed HBM arena exhausted")
        return first

    def sparse(self, task):
        task.first = self.now()
        pages = task.pages
        if len(pages) > SPARSE_SLOT_PAGES:
            raise ValueError("sparse request exceeds its DRAM staging window")
        offset = SLOT_PAGES - SPARSE_SLOT_PAGES
        stats = self.stage(pages, offset, SPARSE)
        self.upload_sparse(task, stats)

    def upload_sparse(self, task, stats):
        pages = task.pages
        offset = SLOT_PAGES - SPARSE_SLOT_PAGES
        first = self.reserve(pages)
        task.finished, copy_ns = self.copy(offset, first, len(pages), SPARSE)
        self.task_io[task.number] = dict(stats, h2d_ns=copy_ns)
        task.locations.update({p: (first + i) * self.size for i, p in enumerate(pages)})

    def take_sparse(self):
        self.admit()
        task = next((r for r in self.pending if r.profile == SPARSE), None)
        if task is not None:
            self.pending.remove(task)
        return task

    def yield_one_sparse(self):
        task = self.take_sparse()
        if task is not None:
            self.sparse(task)
            self.counters["sparse_yields"] += 1

    def start_sparse_read(self):
        if self.pending_read is not None:
            return
        task = self.take_sparse()
        if task is not None:
            if len(task.pages) > SPARSE_SLOT_PAGES:
                raise ValueError("sparse request exceeds its DRAM staging window")
            task.first = self.now()
            self.pending_read = (
                task,
                self.reader.submit(self.read_at, task.pages, SLOT_PAGES - SPARSE_SLOT_PAGES),
            )

    def finish_sparse_read(self, wait=False):
        if self.pending_read is None:
            return
        task, future = self.pending_read
        if wait or future.done():
            trace = future.result()
            stats = self.record_read(trace, SPARSE)
            stats["h2d_wait_ns"] = self.now() - trace[2]
            self.upload_sparse(task, stats)
            self.pending_read = None
            self.counters["sparse_yields"] += 1

    def prefix(self, first_task):
        cohort = [first_task]
        for task in list(self.pending):
            if task.profile == PREFIX and task.arrival == first_task.arrival:
                self.pending.remove(task)
                cohort.append(task)
        pages = tuple(sorted({p for task in cohort for p in task.pages}))
        if len(pages) > SLOT_PAGES - SPARSE_SLOT_PAGES:
            raise ValueError("prefix batch exceeds its DRAM staging window")
        first = self.reserve(pages)
        self.counters["shared_pages_saved"] += sum(len(task.pages) for task in cohort) - len(pages)
        for task in cohort:
            task.first = self.now()
        if self.policy != "read-h2d-yield":
            self.stage(pages, 0, PREFIX)
        if self.policy == "h2d-yield":
            self.yield_one_sparse()
        if self.policy == "h2d-pipeline":
            self.start_sparse_read()
        quantum = len(pages) if self.policy == "strata" else self.quantum
        for offset in range(0, len(pages), quantum):
            current = pages[offset : offset + quantum]
            if self.policy == "read-h2d-yield":
                self.stage(current, offset, PREFIX)
            done, _ = self.copy(offset, first + offset, len(current), PREFIX)
            copied = {p: (first + offset + i) * self.size for i, p in enumerate(current)}
            for task in cohort:
                task.locations.update({p: copied[p] for p in task.pages if p in copied})
                if task.finished is None and len(task.locations) == len(task.pages):
                    task.finished = done
            if offset + len(current) < len(pages):
                if self.policy == "h2d-pipeline":
                    self.finish_sparse_read()
                    self.start_sparse_read()
                else:
                    self.yield_one_sparse()
        self.finish_sparse_read(wait=True)

    def run(self):
        self.origin = time.perf_counter_ns()
        while self.cursor < len(self.requests) or self.pending:
            self.admit()
            if self.pending:
                task = self.pending.popleft()
                if task.profile == PREFIX:
                    self.prefix(task)
                else:
                    self.sparse(task)
            elif self.cursor < len(self.requests):
                wait_ns = self.requests[self.cursor].arrival - self.now()
                time.sleep(max(0, wait_ns) / 1e9)
        self.events.sort(key=lambda event: event[2])
        return max(r.finished for r in self.requests)

    def verify(self, seed, full):
        tested = 0
        for kind in (PREFIX, SPARSE):
            group = [r for r in self.requests if r.profile == kind]
            if not group:
                continue
            samples = group if full else (group[0], group[len(group) // 2], group[-1])
            for task in samples:
                for p in task.pages:
                    actual = self.device.download(self.base + task.locations[p], self.size)
                    if actual != page_bytes(seed, p, self.size):
                        raise ValueError(f"HBM page check failed for task {task.number}, page {p}")
                    tested += 1
        return tested

    def result(self, wall, seed, full):
        rows = []
        for task in self.requests:
            row = {
                "id": task.number,
                "profile": task.profile,
                "arrival_ns": task.arrival,
                "start_ns": task.first,
                "ready_ns": task.finished,
                "pages": task.pages,
            }
            if task.profile == SPARSE:
                blocked = Counter()
                for owner, phase, start, stop in self.events:
                    overlap = min(stop, task.first) - max(start, task.arrival)
                    if overlap > 0:
                        label = "prefix" if owner == PREFIX else "sparse"
                        blocked[f"{label}_{phase}_ns"] += overlap
                row.update(self.task_io[task.number], blocking=dict(blocked))
            rows.append(row)
        groups = {}
        for kind in (PREFIX, SPARSE):
            group = [r for r in self.requests if r.profile == kind]
            if group:
                groups[kind] = {
                    "requests": len(group),
                    "p95_ms": percentile([(r.finished - r.arrival) / 1e6 for r in group], 0.95),
                    "queue_p95_ms": percentile([(r.first - r.arrival) / 1e6 for r in group], 0.95),
                }
        logical = sum(len(r.pages) for r in self.requests) * self.size
        counters = dict(
            self.counters,
            wall_ns=wall,
            logical_bytes=logical,
            verified_pages=self.verify(seed, full),
        )
        return {
            "classes": groups,
            "requests": rows,
            "counts": counters,
            "stage_intervals": self.events,
            "logical_gib_s": logical / GIB / (wall / 1e9),
        }


def cases(suite):
    if suite in ("check", "solve"):
        return [("mix-balanced", p) for p in POLICIES]
    if suite == "pipeline-check":
        return [("mix-balanced", "h2d-pipeline")]
    if suite == "diagnose":
        return [
            ("sparse-only", "strata"),
            ("sparse-matched", "strata"),
            ("mix-balanced", "strata"),
            ("saturated-mix", "strata"),
        ]
    if suite == "diagnose-low":
        return [
            ("sparse-matched", "strata"),
            ("mix-balanced", "strata"),
            ("saturated-sparse", "strata"),
        ]
    if suite == "direct-confirm":
        return [("sparse-matched", "strata"), ("saturated-sparse", "strata")] + [
            ("mix-balanced", p) for p in POLICIES
        ]
    if suite == "pipeline":
        return [("mix-balanced", p) for p in PIPELINE_POLICIES]
    return [("mix-sparse-heavy", p) for p in PIPELINE_POLICIES]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--suite",
        choices=(
            "check",
            "pipeline-check",
            "diagnose",
            "diagnose-low",
            "solve",
            "direct-confirm",
            "pipeline",
            "robustness",
        ),
        required=True,
    )
    parser.add_argument("--seeds", nargs="+", type=int, default=(42, 43, 44))
    parser.add_argument("--io", choices=("buffered", "direct"), default="buffered")
    parser.add_argument("--rate", type=float, default=0.16)
    parser.add_argument("--scale", type=int, default=2)
    parser.add_argument("--quantum", type=int, default=8)
    parser.add_argument("--device", default="0")
    args = parser.parse_args()
    if not math.isfinite(args.rate) or min(args.rate, args.scale, args.quantum) <= 0:
        parser.error("rate, scale and quantum must be positive")
    args.output.mkdir(parents=True)
    spec, corpus_seed = read_manifest(args.corpus)
    with ExitStack() as stack:
        device = stack.enter_context(CPU() if args.device == "cpu" else Ascend(int(args.device)))
        base = stack.enter_context(device.allocate(ARENA_MIB * MIB))
        staging = HostBuffer(SLOT_PAGES * spec.page_bytes)
        stack.callback(staging.close)
        fd = open_corpus(args.corpus, "page-first", spec, args.io)
        stack.callback(os.close, fd)
        reader = stack.enter_context(ThreadPoolExecutor(max_workers=1))
        for seed in args.seeds:
            schedule = cases(args.suite)
            random.Random(seed).shuffle(schedule)
            for scenario, policy in schedule:
                stress_traces = {
                    "saturated-mix": "mix-balanced",
                    "saturated-sparse": "sparse-matched",
                }
                offered = 80 if scenario in stress_traces else args.rate
                trace = arrival_trace(
                    stress_traces.get(scenario, scenario),
                    seed,
                    spec.page_bytes,
                    offered,
                    args.scale,
                )
                trial = Trial(trace, fd, spec, device, base, staging, policy, args.quantum, reader)
                print(args.io, seed, scenario, policy, flush=True)
                wall = trial.run()
                row = trial.result(wall, corpus_seed, args.suite in ("check", "pipeline-check"))
                row.update(
                    io=args.io,
                    scenario=scenario,
                    policy=policy,
                    seed=seed,
                    nominal_mix_gib_s=offered,
                    page_bytes=spec.page_bytes,
                    quantum_pages=args.quantum,
                    scale=args.scale,
                    backend="cpu" if args.device == "cpu" else "ascend",
                )
                write_json(row, args.output / f"{seed}.{scenario}.{policy}.json")
                print(f"  {row['logical_gib_s']:.3f} GiB/s", row["classes"], flush=True)


if __name__ == "__main__":
    main()
