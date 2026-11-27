"""Plan shared-prefix reads and H2D copies against per-request completion times."""

import time
from concurrent.futures import TimeoutError
from dataclasses import dataclass
from statistics import median

from superload.dataset.corpus import make_plan

from .trace import PREFIX, SPARSE


JOINT_POLICIES = ("fixed-8", "fixed-16", "joint-h2d", "joint")


@dataclass(frozen=True)
class Costs:
    read_page: float
    read_extent: float
    copy_launch: float
    copy_page: float

    def read(self, pages, extents):
        return pages * self.read_page + extents * self.read_extent

    def copy(self, pages):
        return self.copy_launch + pages * self.copy_page


def calibrate(trial):
    """Measure aligned contiguous reads, scattered reads, and two copy sizes."""
    trial.origin = time.perf_counter_ns()
    contiguous = tuple(range(8))
    scattered = tuple(range(192, 208, 2))
    read_times = []
    for pages in (contiguous, scattered):
        observations = []
        for _ in range(3):
            _, start, end, _, _ = trial.read_at(pages, 0)
            observations.append(end - start)
        read_times.append(median(observations))
    read_extent = min(read_times[0] / 2, max(0, (read_times[1] - read_times[0]) / 7))
    read_page = max(1, (read_times[0] - read_extent) / 8)
    copy_times = []
    for pages in (8, 32):
        observations = []
        for _ in range(3):
            started = time.perf_counter_ns()
            trial.device.upload(trial.base, trial.staging.address, pages * trial.size)
            observations.append(time.perf_counter_ns() - started)
        copy_times.append(median(observations))
    copy_page = max(1, min(copy_times[0] / 8, (copy_times[1] - copy_times[0]) / 24))
    return Costs(read_page, read_extent, max(0, copy_times[0] - 8 * copy_page), copy_page)


@dataclass
class Group:
    kind: str
    tasks: tuple
    pages: tuple
    staging: int
    hbm: int
    dependencies: tuple
    read_pages: int = 0
    copied_pages: int = 0


@dataclass(frozen=True)
class Operation:
    group: str
    start: int
    end: int
    duration: float


class JointSchedule:
    def __init__(self, trial, costs, prefix_capacity, sparse_capacity):
        self.trial = trial
        self.costs = costs
        self.prefix_capacity = prefix_capacity
        self.sparse_capacity = sparse_capacity
        self.groups = {}
        self.reading = None

    def admit(self):
        trial = self.trial
        trial.admit()
        for first in tuple(trial.pending):
            if first.profile in self.groups:
                continue
            if first.profile == PREFIX:
                tasks = tuple(
                    req
                    for req in trial.pending
                    if req.profile == PREFIX and req.arrival == first.arrival
                )
                pages = tuple(sorted({page for req in tasks for page in req.pages}))
                offset, capacity = 0, self.prefix_capacity
                trial.counters["shared_pages_saved"] += sum(len(req.pages) for req in tasks) - len(
                    pages
                )
            else:
                tasks, pages = (first,), first.pages
                offset, capacity = self.prefix_capacity, self.sparse_capacity
            if len(pages) > capacity:
                raise ValueError("request group exceeds its DRAM staging window")
            indices = {page: i + 1 for i, page in enumerate(pages)}
            dependencies = tuple(
                (req, max(indices[p] for p in req.pages), self.solo_cost(req.pages))
                for req in tasks
            )
            for req in tasks:
                trial.pending.remove(req)
            self.groups[first.profile] = Group(
                first.profile, tasks, pages, offset, trial.reserve(pages), dependencies
            )

    def solo_cost(self, pages):
        extents = len(make_plan("page-first", self.trial.spec, pages))
        return self.costs.read(len(pages), extents) + self.costs.copy(len(pages))

    def finish_read(self):
        operation, _, future = self.reading
        raw = future.result()
        group = self.groups[operation.group]
        stats = self.trial.record_read(raw, group.kind)
        group.read_pages = operation.end
        if group.kind == SPARSE:
            self.trial.task_io[group.tasks[0].number] = dict(stats, read_ready_ns=raw[2])
        self.reading = None

    def submit_read(self, operation):
        group = self.groups[operation.group]
        submitted = self.trial.now()
        for task in group.tasks:
            if task.first is None:
                task.first = submitted
        future = self.trial.reader.submit(
            self.trial.read_at,
            group.pages[operation.start : operation.end],
            group.staging + operation.start,
        )
        self.reading = operation, submitted, future

    def copy(self, operation):
        group = self.groups[operation.group]
        started = self.trial.now()
        done, duration = self.trial.copy(
            group.staging + operation.start,
            group.hbm + operation.start,
            operation.end - operation.start,
            group.kind,
        )
        group.copied_pages = operation.end
        if group.kind == SPARSE:
            stats = self.trial.task_io[group.tasks[0].number]
            stats["h2d_wait_ns"] = max(0, started - stats.pop("read_ready_ns"))
            stats["h2d_ns"] = duration
        for task, end, _ in group.dependencies:
            if task.finished is None and end <= operation.end:
                task.locations = {
                    page: (group.hbm + i) * self.trial.size
                    for i, page in enumerate(group.pages)
                    if page in task.pages
                }
                task.finished = done
        if operation.end == len(group.pages):
            del self.groups[group.kind]

    def operation(self, group, start, end, read):
        if read:
            extents = len(make_plan("page-first", self.trial.spec, group.pages[start:end]))
            duration = self.costs.read(end - start, extents)
        else:
            duration = self.costs.copy(end - start)
        return Operation(group.kind, start, end, duration)

    def orders(self, read):
        """Enumerate a single optional prefix split and sparse insertion per stage."""
        prefix = self.groups.get(PREFIX)
        sparse = self.groups.get(SPARSE)
        inflight = self.reading[0] if read and self.reading else None

        def start(group):
            if read:
                return (
                    inflight.end if inflight and inflight.group == group.kind else group.read_pages
                )
            return group.copied_pages

        pending_sparse = None
        if sparse and start(sparse) < len(sparse.pages):
            pending_sparse = self.operation(sparse, start(sparse), len(sparse.pages), read)
        if not prefix or start(prefix) == len(prefix.pages):
            return [(pending_sparse,) if pending_sparse else ()]

        begin, end = start(prefix), len(prefix.pages)
        cuts = {boundary for _, boundary, _ in prefix.dependencies if begin < boundary < end}
        if read:
            if self.trial.policy == "joint-h2d":
                cuts.clear()
            else:
                # Read splits between extents keep the same underlying preadv ranges.
                extent_ends = [
                    i for i in range(begin + 1, end) if prefix.pages[i - 1] + 1 != prefix.pages[i]
                ]
                cuts = {
                    next((i for i in extent_ends if i >= boundary), end) for boundary in cuts
                } - {end}
                if pending_sparse and extent_ends:
                    cuts.add(extent_ends[0])
        elif begin < prefix.read_pages < end:
            cuts.add(prefix.read_pages)

        orders = []
        for cut in (None, *sorted(cuts)):
            bounds = (begin, end) if cut is None else (begin, cut, end)
            chain = tuple(self.operation(prefix, a, b, read) for a, b in zip(bounds, bounds[1:]))
            if pending_sparse:
                orders.extend(
                    chain[:i] + (pending_sparse,) + chain[i:] for i in range(len(chain) + 1)
                )
            else:
                orders.append(chain)
        return orders

    def plan(self, now):
        h2d_orders = self.orders(read=False)
        best_score, selected = None, (None, None)
        for reads in self.orders(read=True):
            read_at = {kind: [(group.read_pages, now)] for kind, group in self.groups.items()}
            read_end = now
            if self.reading:
                active, submitted, _ = self.reading
                read_end = max(now, submitted + active.duration)
                read_at[active.group].append((active.end, read_end))
            for item in reads:
                read_end += item.duration
                read_at[item.group].append((item.end, read_end))

            for copies in h2d_orders:
                h2d_end = now
                ready = {}
                for item in copies:
                    staged_at = next(t for end, t in read_at[item.group] if end >= item.end)
                    h2d_end = max(h2d_end, staged_at) + item.duration
                    for task, last_page, solo in self.groups[item.group].dependencies:
                        if task.finished is None and last_page <= item.end:
                            ready.setdefault(task.number, (h2d_end - task.arrival) / solo)
                score = (
                    max(ready.values()),
                    sum(ready.values()),
                    h2d_end,
                    len(reads) + len(copies),
                )
                if best_score is None or score < best_score:
                    best_score = score
                    selected = (reads[0] if reads else None, copies[0])
        return selected

    def fixed(self):
        quantum = int(self.trial.policy.split("-")[1])
        first_read, first_copy = None, None
        for kind in (SPARSE, PREFIX):
            group = self.groups.get(kind)
            if group is None:
                continue
            chunk = len(group.pages) if kind == SPARSE else quantum
            if first_read is None and group.read_pages < len(group.pages):
                first_read = self.operation(
                    group, group.read_pages, min(len(group.pages), group.read_pages + chunk), True
                )
            if first_copy is None and group.copied_pages < group.read_pages:
                first_copy = self.operation(
                    group,
                    group.copied_pages,
                    min(group.read_pages, group.copied_pages + chunk),
                    False,
                )
        return first_read, first_copy

    def run(self):
        trial = self.trial
        trial.origin = time.perf_counter_ns()
        while trial.cursor < len(trial.requests) or trial.pending or self.groups:
            if self.reading and self.reading[2].done():
                self.finish_read()
            self.admit()
            if self.groups:
                started = trial.now()
                first_read, first_copy = (
                    self.fixed() if trial.policy.startswith("fixed-") else self.plan(started)
                )
                trial.counters.update(decisions=1, scheduler_ns=trial.now() - started)
                if first_read and not self.reading:
                    prefix = self.groups[first_read.group]
                    if prefix.kind == PREFIX and first_read.end < len(prefix.pages):
                        trial.counters["prefix_read_splits"] += 1
                    self.submit_read(first_read)
                if first_copy and first_copy.end <= self.groups[first_copy.group].read_pages:
                    prefix = self.groups[first_copy.group]
                    if prefix.kind == PREFIX and first_copy.end < len(prefix.pages):
                        trial.counters["prefix_h2d_splits"] += 1
                    self.copy(first_copy)
                    continue
            next_arrival = (
                max(0, trial.requests[trial.cursor].arrival - trial.now()) / 1e9
                if trial.cursor < len(trial.requests)
                else None
            )
            if self.reading:
                try:
                    self.reading[2].result(timeout=next_arrival)
                except TimeoutError:
                    pass
            elif next_arrival is not None:
                time.sleep(next_arrival)
            else:
                raise RuntimeError("no executable transfer for pending requests")
        trial.events.sort(key=lambda event: event[2])
        return max(r.finished for r in trial.requests)
