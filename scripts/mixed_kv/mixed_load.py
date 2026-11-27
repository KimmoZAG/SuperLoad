"""Open-loop mixed KV reload experiments using SuperLoad's storage and device APIs."""
import argparse
import json
import math
import os
import random
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

from scripts.run_matrix import roce_storage
from superload.benchmark.metrics import percentile
from superload.cli import write_json
from superload.dataset.corpus import make_plan, open_corpus, page_bytes, read_manifest, read_spans
from superload.hardware.device import Ascend, CPU, HostBuffer
from superload.schemes.roce import RoceSource

from .trace import DEADLINE_MS, GIB, MIB, PREFIX, SPARSE, arrival_trace


CHUNK_PAGES = 4
PREFETCH_PAGES = 2
TRANSPORTS = ("local", "roce")


@dataclass
class Chunk:
    owner: int
    pages: tuple
    buffer: HostBuffer
    speculative: bool = False
    future: object = None
    issued: int = 0
    fetched: int = 0
    stats: dict = field(default_factory=dict)
    waited_for_upload: bool = False


class Replay:
    def __init__(self, requests, fetch, buffers, device, base, arena, page_size, policy):
        self.requests = requests
        self.fetch = fetch
        self.free = list(buffers)
        self.device = device
        self.base = base
        self.arena = arena
        self.page_size = page_size
        self.policy = policy
        self.merge = policy not in ("fifo", "feedback-no-merge")
        self.prefetch = policy not in ("fifo", "feedback-no-prefetch")
        self.feedback = policy.startswith("feedback")
        self.active = {}
        self.ready = []
        self.reading = None
        self.predictions = {}
        self.prefetched = {}
        self.last_sparse = {}
        self.next_request = 0
        self.written = 0
        self.fetch_ns = 5_000_000
        self.h2d_ns = 3_000_000
        self.peak_slots = 0
        self.events = []
        self.counts = Counter()

    def now(self):
        return time.perf_counter_ns() - self.start

    def complete(self, request, now):
        if len(request.locations) != len(request.pages):
            return
        request.finished = now
        self.active.pop(request.number)
        if (
            self.prefetch
            and request.profile == SPARSE
            and self.last_sparse[request.session] == request.number
        ):
            self.predictions[request.session] = (request.number, request.selected[:PREFETCH_PAGES])

    def admit(self, now):
        while self.next_request < len(self.requests):
            request = self.requests[self.next_request]
            if request.arrival > now:
                return
            self.next_request += 1
            self.active[request.number] = request
            if request.profile == SPARSE:
                self.last_sparse[request.session] = request.number
                self.predictions.pop(request.session, None)
                prior = self.prefetched.pop(request.session, {})
                for page in request.pages:
                    if page in prior:
                        request.locations[page] = prior[page]
                        request.prefetched_pages += 1
                if request.prefetched_pages:
                    request.first = request.arrival
            self.complete(request, now)

    def key(self, request):
        if not self.feedback:
            return request.deadline if self.policy == "ingress-edf" else request.number
        ready_pages = {
            page
            for chunk in self.ready
            for page in chunk.pages
            if self.merge or chunk.owner == request.number
        }
        remaining = set(request.pages) - request.locations.keys()
        need_fetch = len(remaining - ready_pages)
        slack = request.deadline - len(remaining) * self.h2d_ns - need_fetch * self.fetch_ns
        return slack, request.number

    def choose_fetch(self, pool):
        if not self.free or self.reading:
            return
        reserved = {
            (None if self.merge else job.owner, page) for job in self.ready for page in job.pages
        }
        for req in sorted(self.active.values(), key=self.key):
            owner = None if self.merge else req.number
            pages = tuple(
                p for p in req.pages if p not in req.locations and (owner, p) not in reserved
            )[:CHUNK_PAGES]
            if pages:
                self.issue(pool, Chunk(req.number, pages, self.free.pop()))
                return
        if (
            not self.prefetch
            or self.next_request == len(self.requests)
            or not self.predictions
            or (
                self.feedback
                and (
                    self.active
                    or PREFETCH_PAGES * (self.fetch_ns + self.h2d_ns)
                    > DEADLINE_MS[SPARSE] * 500_000
                )
            )
        ):
            return
        session = min(self.predictions, key=lambda s: self.predictions[s][0])
        owner, pages = self.predictions.pop(session)
        if pages:
            self.issue(pool, Chunk(owner, pages, self.free.pop(), speculative=True))

    def issue(self, pool, job):
        job.issued = self.now()
        job.future = pool.submit(self.fetch_timed, job)
        self.reading = job
        self.peak_slots = max(self.peak_slots, len(self.ready) + 1)

    def fetch_timed(self, job):
        start = self.now()
        stats = self.fetch(job.pages, job.buffer)
        return self.now(), start, stats

    def collect(self):
        if self.reading and self.reading.future.done():
            job, self.reading = self.reading, None
            job.fetched, start, job.stats = job.future.result()
            job.stats["fetch_worker_queue_ns"] = start - job.issued
            job.stats["fetch_ns"] = job.fetched - start
            sample = job.stats["fetch_ns"] / len(job.pages)
            self.fetch_ns = (self.fetch_ns * 3 + sample) / 4
            self.ready.append(job)

    def upload_priority(self, job):
        attached = [
            r
            for r in self.active.values()
            if (self.merge or r.number == job.owner)
            and set(job.pages).intersection(r.pages) - r.locations.keys()
        ]
        return min((self.key(r) for r in attached), default=(float("inf"), job.owner))

    def wait_for_urgent_fetch(self):
        if not self.feedback or not self.reading or len(self.ready) != 1:
            return False
        ready = self.ready[0]
        urgent_slack, _ = self.upload_priority(self.reading)
        ready_slack, _ = self.upload_priority(ready)
        wait_ns = min(5_000_000, ready_slack - self.now())
        if ready.waited_for_upload or urgent_slack >= ready_slack or wait_ns <= 0:
            return False
        ready.waited_for_upload = True
        before = self.now()
        try:
            self.reading.future.result(timeout=wait_ns / 1e9)
        except TimeoutError:
            pass
        self.counts["feedback_wait_ns"] += self.now() - before
        return True

    def upload(self):
        # Ready chunks share one H2D lane. The feedback policy can reorder this lane.
        index = 0
        if self.feedback and len(self.ready) > 1:
            index = min(range(len(self.ready)), key=lambda i: self.upload_priority(self.ready[i]))
        job = self.ready.pop(index)
        n = len(job.pages) * self.page_size
        if self.written + n > self.arena:
            raise RuntimeError("experiment exhausted the fixed HBM arena")
        start = self.now()
        self.device.upload(self.base + self.written, job.buffer.address, n)
        done = self.now()
        job.stats.update(
            h2d_ns=done - start,
            h2d_queue_ns=start - job.fetched,
            hbm_bytes=n,
            prefetch_bytes=n if job.speculative else 0,
        )
        self.h2d_ns = (self.h2d_ns * 3 + job.stats["h2d_ns"] / len(job.pages)) / 4
        self.free.append(job.buffer)
        positions = {p: self.written + i * self.page_size for i, p in enumerate(job.pages)}
        self.written += n
        self.admit(done)
        if job.speculative:
            session = self.requests[job.owner].session
            if self.last_sparse[session] == job.owner:
                self.prefetched[session] = positions
        consumers = Counter()
        for request in list(self.active.values()):
            if not self.merge and request.number != job.owner:
                continue
            needed = set(request.pages).intersection(positions) - request.locations.keys()
            if not needed:
                continue
            served_at = max(request.arrival, job.issued)
            request.first = served_at if request.first is None else min(request.first, served_at)
            request.locations.update({p: positions[p] for p in needed})
            if job.speculative:
                request.late_prefetch_pages += len(needed)
            consumers.update(needed)
            self.complete(request, done)
        self.counts.update(job.stats)
        self.counts["shared_transfers_saved"] += sum(n - 1 for n in consumers.values())
        self.events.append(
            {
                "owner": job.owner,
                "pages": job.pages,
                "speculative": job.speculative,
                "issued_ns": job.issued,
                "ready_ns": job.fetched,
                "completed_ns": done,
                **job.stats,
            }
        )

    def run(self, pool):
        self.start = time.perf_counter_ns()
        while self.next_request < len(self.requests) or self.active or self.reading or self.ready:
            now = self.now()
            self.admit(now)
            self.collect()
            self.choose_fetch(pool)
            if self.ready:
                if not self.wait_for_urgent_fetch():
                    self.upload()
                continue
            until = (
                (self.requests[self.next_request].arrival - self.now()) / 1e9
                if self.next_request < len(self.requests)
                else None
            )
            if self.reading:
                try:
                    self.reading.future.result(timeout=max(0, until) if until is not None else None)
                except TimeoutError:
                    pass
            elif until is not None:
                time.sleep(max(0, until))
            elif self.active:
                raise RuntimeError("demand remains with no possible transfer")
        return self.events


def report_replay(replay, spec, corpus_seed, verify_all):
    reqs = replay.requests
    tested = set()
    for profile in (PREFIX, SPARSE):
        group = [r for r in reqs if r.profile == profile]
        if group:
            if verify_all:
                tested.update(r.number for r in group)
            else:
                tested.update((group[0].number, group[len(group) // 2].number, group[-1].number))
            reused = next((r for r in group if r.prefetched_pages), None)
            if reused:
                tested.add(reused.number)
    for number in sorted(tested):
        r = reqs[number]
        for p in r.pages:
            raw = replay.device.download(replay.base + r.locations[p], spec.page_bytes)
            if raw != page_bytes(corpus_seed, p, spec.page_bytes):
                raise RuntimeError(f"HBM verification failed for request {number}, page {p}")
    wall_ns = max(e["completed_ns"] for e in replay.events)
    rows = [
        {
            "id": r.number,
            "profile": r.profile,
            "session": r.session,
            "arrival_ns": r.arrival,
            "deadline_ns": r.deadline,
            "first_ns": r.first,
            "ready_ns": r.finished,
            "pages": r.pages,
            "prefetch_hits": r.prefetched_pages,
            "late_prefetch_hits": r.late_prefetch_pages,
        }
        for r in reqs
    ]
    summary = {}
    for kind in (PREFIX, SPARSE):
        selected = [r for r in reqs if r.profile == kind]
        if not selected:
            continue
        samples = [(r.finished - r.arrival) / 1e6 for r in selected]
        summary[kind] = {
            "requests": len(selected),
            "p50_ms": percentile(samples, 0.5),
            "p95_ms": percentile(samples, 0.95),
            "p99_ms": percentile(samples, 0.99),
            "queue_p95_ms": percentile([(r.first - r.arrival) / 1e6 for r in selected], 0.95),
            "on_time": sum(r.finished <= r.deadline for r in selected) / len(selected),
        }
    logical_bytes = sum(len(r.pages) for r in reqs) * spec.page_bytes
    counts = dict(
        replay.counts,
        wall_ns=wall_ns,
        logical_bytes=logical_bytes,
        peak_slots=replay.peak_slots,
        verified_pages=sum(len(reqs[i].pages) for i in tested),
    )
    return {
        "classes": summary,
        "counts": counts,
        "ready_gib_s": logical_bytes / GIB / (wall_ns / 1e9),
        "requests": rows,
        "chunks": replay.events,
    }


def open_fetch(stack, args, transport, spec, seed, device):
    if transport == "local":
        fd = open_corpus(args.corpus, "layer-major", spec, args.io)
        stack.callback(os.close, fd)

        def fetch(pages, buffer):
            start = time.perf_counter_ns()
            plan = make_plan("layer-major", spec, pages)
            read_start = time.perf_counter_ns()
            calls, amount = read_spans(fd, buffer.view, plan)
            return {
                "storage_bytes": amount,
                "read_calls": calls,
                "disk_ns": time.perf_counter_ns() - read_start,
                "staging_ns": read_start - start,
            }

        return fetch
    flags = stack.enter_context(roce_storage(args))
    port = int(flags[1])
    server = SimpleNamespace(**vars(args))
    server.control_host, server.control_port = "127.0.0.1", port
    source = stack.enter_context(
        RoceSource(server, spec, seed, device, CHUNK_PAGES, external_buffer=True)
    )
    return source.receive


def campaign(suite):
    if suite == "smoke":
        return [(t, "mix-balanced", "feedback") for t in TRANSPORTS]
    if suite == "local-smoke":
        return [
            ("local", "mix-balanced", p)
            for p in (
                "fifo",
                "static",
                "ingress-edf",
                "feedback",
                "feedback-no-merge",
                "feedback-no-prefetch",
            )
        ]
    topology = [
        (t, s, "fifo")
        for t in TRANSPORTS
        for s in ("prefix-only", "sparse-only", "mix-balanced", "mix-sparse-heavy")
    ]
    matched = [("roce", s, "fifo") for s in ("prefix-matched", "sparse-matched")]
    policies = [
        ("roce", s, p)
        for s in ("mix-balanced", "mix-sparse-heavy")
        for p in ("static", "ingress-edf", "feedback")
    ]
    ablations = [("roce", "mix-balanced", p) for p in ("feedback-no-merge", "feedback-no-prefetch")]
    if suite == "direct-check":
        return [("roce", "mix-balanced", p) for p in ("fifo", "static", "ingress-edf", "feedback")]
    return topology + matched + policies + ablations


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--suite", choices=("main", "direct-check", "smoke", "local-smoke"), default="main"
    )
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--device", default="0")
    parser.add_argument("--io", choices=("buffered", "direct"), default="buffered")
    parser.add_argument("--offered-gib-s", type=float, default=0.28)
    parser.add_argument("--arena-mib", type=int, default=4096)
    parser.add_argument("--seeds", type=int, nargs="+", default=(42, 43, 44))
    parser.add_argument("--scale", type=int, default=1)
    parser.add_argument("--hca", default="mlx5_2")
    parser.add_argument("--gid-index", type=int, default=5)
    args = parser.parse_args()
    args.ib_port, args.mr_bytes, args.timeout_ms = 1, 32768, 8000
    args.bridge = "build/native/libsuperload_roce.so"
    if (
        args.scale <= 0
        or args.arena_mib <= 0
        or not math.isfinite(args.offered_gib_s)
        or args.offered_gib_s <= 0
    ):
        parser.error("scale, arena capacity, and offered load must be positive")
    if args.output.exists():
        parser.error("choose a new output directory")
    args.output.mkdir(parents=True)
    spec, corpus_seed = read_manifest(args.corpus)
    arena = args.arena_mib * MIB
    chunk_bytes = CHUNK_PAGES * spec.page_bytes
    with ExitStack() as stack:
        device = stack.enter_context(CPU() if args.device == "cpu" else Ascend(int(args.device)))
        base = stack.enter_context(device.allocate(arena))
        buffers = [HostBuffer(chunk_bytes) for _ in range(2)]
        for buf in buffers:
            stack.callback(buf.close)
        pool = stack.enter_context(ThreadPoolExecutor(max_workers=1))
        for seed in args.seeds:
            cells = campaign(args.suite)
            random.Random(seed).shuffle(cells)
            for transport, scenario, policy in cells:
                print(f"{seed} {transport} {scenario} {policy}", flush=True)
                offered = args.offered_gib_s if args.suite not in ("smoke", "local-smoke") else 40
                requests = arrival_trace(scenario, seed, spec.page_bytes, offered, args.scale)
                if sum(len(r.pages) + PREFETCH_PAGES for r in requests) * spec.page_bytes > arena:
                    raise RuntimeError("increase the common arena to fit the prescribed trace")
                with ExitStack() as channel:
                    fetch = open_fetch(channel, args, transport, spec, corpus_seed, device)
                    replay = Replay(
                        requests, fetch, buffers, device, base, arena, spec.page_bytes, policy
                    )
                    replay.run(pool)
                result = report_replay(
                    replay, spec, corpus_seed, args.suite in ("smoke", "local-smoke")
                )
                result.update(
                    seed=seed,
                    transport=transport,
                    scenario=scenario,
                    policy=policy,
                    io=args.io,
                    backend="cpu" if args.device == "cpu" else "ascend",
                    corpus_seed=corpus_seed,
                    page_bytes=spec.page_bytes,
                    deadline_ms=DEADLINE_MS,
                    slots=len(buffers),
                    slot_bytes=chunk_bytes,
                    mr_bytes=args.mr_bytes,
                    arena_bytes=arena,
                    nominal_mix_gib_s=offered,
                    cpu_mask=sorted(os.sched_getaffinity(0)),
                )
                path = args.output / f"{seed}.{transport}.{scenario}.{policy}.json"
                write_json(result, path)
                print("  " + json.dumps(result["classes"], separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
