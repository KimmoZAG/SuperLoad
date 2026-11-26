"""Replay a trace, time KV readiness, and verify the resulting bytes."""

import os
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from statistics import median

from .corpus import interleave, make_plan, open_corpus, page_bytes, page_extents, read_spans
from .device import Ascend, CPU, HostBuffer
from .policies import HostCache
from .roce import RoceSource
from .workloads import make_trace


SCHEMES = ("native", "roce", "strata", "bidaw", "solid")
CONTROLS = ("native-lru", "strata-io", "solid-io")


class LocalSource:
    def __init__(self, fd, stager, device, spec, layout):
        self.fd = fd
        self.stager = stager
        self.device = device
        self.spec = spec
        self.layout = layout

    def load(self, pages, dest, cache=None, request=None, prefetched=None):
        length = len(pages) * self.spec.page_bytes
        metrics = Counter()
        start = time.perf_counter_ns()
        if cache is None and prefetched is None:
            spans = make_plan(self.layout, self.spec, pages)
        else:
            spans = []
            misses = []
            for i, page in enumerate(pages):
                target = i * self.spec.page_bytes
                cached = cache.get(page) if cache else prefetched.get(page)
                if cached is None:
                    spans.extend(page_extents(self.layout, self.spec, page, target))
                    misses.append((page, target))
                else:
                    self.stager.view[target:target + self.spec.page_bytes] = cached
                    key = "host_hit_pages" if cache else "prefetch_hit_pages"
                    metrics[key] += 1
        read_start = time.perf_counter_ns()
        calls, amount = read_spans(self.fd, self.stager.view, spans)
        read_done = time.perf_counter_ns()
        if cache and cache.capacity:
            for page, target in misses:
                data = bytes(self.stager.view[target:target + self.spec.page_bytes])
                owner = request.session if page >= 4 or request.selected else None
                cache.put(page, data, owner)
        copy_start = time.perf_counter_ns()
        self.device.upload(dest, self.stager.address, length)
        copy_done = time.perf_counter_ns()
        metrics.update(
            storage_bytes=amount, read_calls=calls, disk_ns=read_done - read_start,
            hbm_bytes=length, h2d_ns=copy_done - copy_start,
            staging_ns=(read_start - start) + (copy_start - read_done),
        )
        return metrics

    def prefetch(self, pages):
        start = time.perf_counter_ns()
        step = self.spec.page_bytes
        buffer = HostBuffer(len(pages) * step)
        try:
            calls, amount = read_spans(self.fd, buffer.view, make_plan(self.layout, self.spec, pages))
            values = {page: bytes(buffer.view[i * step:(i + 1) * step]) for i, page in enumerate(pages)}
        finally:
            buffer.close()
        return values, Counter(
            storage_bytes=amount, prefetch_bytes=amount, read_calls=calls, prefetch_calls=calls,
            prefetch_ns=time.perf_counter_ns() - start,
        )


def verify(device, base, snapshot, spec, seed, layout):
    for index, page in enumerate(snapshot):
        raw = device.download(base + index * spec.page_bytes, spec.page_bytes)
        expected = page_bytes(seed, page, spec.page_bytes)
        if layout == "interleaved":
            expected = interleave(spec, expected)
        if raw != expected:
            raise RuntimeError(f"KV validation failed for page {page}, layout {layout}")
    return len(snapshot)


def wave_requests(wave, args, spec, load, base, cache, prefetched):
    metrics = Counter()
    requests = wave.requests
    start = time.perf_counter_ns()
    rows = []
    snapshot = []
    if args.scheme == "strata":
        pages = sorted({page for req in requests for page in req.pages})
        metrics.update(load(pages, base))
        done = time.perf_counter_ns()
        elapsed = (done - start) / 1e6
        for req in requests:
            rows.append({"latency_ms": elapsed, "queue_ms": 0, "pages": len(req.pages)})
        snapshot.extend(pages)
    else:
        if cache is not None:
            requests = cache.schedule(requests)
        done = start
        for req in requests:
            launched = time.perf_counter_ns()
            if cache is not None:
                cache.begin_request(req)
            dest = base + len(snapshot) * spec.page_bytes
            if cache is not None or prefetched is not None:
                counters = load(req.pages, dest, cache=cache, request=req, prefetched=prefetched)
            else:
                counters = load(req.pages, dest)
            done = time.perf_counter_ns()
            metrics.update(counters)
            rows.append({
                "latency_ms": (done - start) / 1e6,
                "queue_ms": (launched - start) / 1e6,
                "pages": len(req.pages),
            })
            snapshot.extend(req.pages)
    metrics["logical_pages"] = sum(row["pages"] for row in rows)
    metrics["active_ns"] = done - start
    return metrics, rows, snapshot


def percentile(values, fraction):
    values = sorted(values)
    which = (len(values) - 1) * fraction
    index = int(which)
    part = which - index
    return values[index] + (values[min(index + 1, len(values) - 1)] - values[index]) * part


def summarize(metrics, rows, spec):
    logical_bytes = metrics["logical_pages"] * spec.page_bytes
    elapsed = metrics["wall_ns"] / 1e9
    ready = [row["latency_ms"] for row in rows]
    queue = [row["queue_ms"] for row in rows]
    return {
        "requests": len(rows),
        "p50_ms": percentile(ready, 0.50),
        "p95_ms": percentile(ready, 0.95),
        "p99_ms": percentile(ready, 0.99),
        "queue_p95_ms": percentile(queue, 0.95),
        "ready_gib_s": logical_bytes / (1 << 30) / elapsed,
        "requests_s": len(rows) / elapsed,
        "read_bytes_per_logical_byte": metrics["storage_bytes"] / logical_bytes,
        "hbm_bytes_per_logical_byte": metrics["hbm_bytes"] / logical_bytes,
        "host_hit_fraction": metrics["host_hit_pages"] / metrics["logical_pages"],
        "prefetch_hit_fraction": metrics["prefetch_hit_pages"] / metrics["logical_pages"],
        "prefetch_use_fraction": (metrics["prefetch_hit_pages"] * spec.page_bytes / metrics["prefetch_bytes"])
        if metrics["prefetch_bytes"] else 0.0,
        "reads_per_request": metrics["read_calls"] / len(rows),
    }


def trial(args, spec, seed, trace, device, base, loader, pool, layout):
    cache = HostCache(args.cache_pages, args.scheme == "bidaw") if args.scheme in ("bidaw", "native-lru") else None
    summary = Counter()
    rows = []
    wave_metrics = []
    previous = ()
    for index, wave in enumerate(trace):
        cycle = time.perf_counter_ns()
        predicted = previous[:args.prefetch_pages] if pool is not None else ()
        future = pool.submit(loader.prefetch, predicted) if predicted else None
        if wave.gap_ms:
            time.sleep(wave.gap_ms / 1000)
        prefetch_counters = Counter()
        values = None
        if future:
            wait = time.perf_counter_ns()
            values, prefetch_counters = future.result()
            prefetch_counters["prefetch_wait_ns"] = time.perf_counter_ns() - wait
        load = loader.load
        counters, ready, snapshot = wave_requests(wave, args, spec, load, base, cache, values)
        counters["wall_ns"] = time.perf_counter_ns() - cycle
        if future:
            # Any prefetch stall past the nominal inter-step gap delays KV readiness.
            for row in ready:
                row["latency_ms"] += prefetch_counters["prefetch_wait_ns"] / 1e6
        counters.update(prefetch_counters)
        previous = wave.requests[-1].selected
        check = args.verify == "all" or index == len(trace) - 1
        if check:
            counters["verified_pages"] = verify(device, base, snapshot, spec, seed, layout)
        if index >= args.warmup:
            summary.update(counters)
            rows.extend(ready)
            wave_metrics.append(dict(counters))
    return {"summary": summarize(summary, rows, spec), "counters": dict(summary),
            "requests": rows, "waves": wave_metrics}


def run(args, spec, seed):
    trace = make_trace(args.profile, args.seed, args.warmup + args.waves)
    max_pages = max(sum(len(req.pages) for req in wave.requests) for wave in trace)
    length = max_pages * spec.page_bytes
    layout = {
        "strata": "page-first", "strata-io": "page-first", "solid": "interleaved", "solid-io": "interleaved",
    }.get(args.scheme, "layer-major")
    use_roce = args.scheme == "roce"
    with ExitStack() as stack:
        device = stack.enter_context(CPU() if args.device == "cpu" else Ascend(int(args.device)))
        base = stack.enter_context(device.allocate(length))
        pool = None
        if use_roce:
            request_pages = max(len(req.pages) for wave in trace for req in wave.requests)
            loader = stack.enter_context(RoceSource(args, spec, seed, device, request_pages))
        else:
            stager = HostBuffer(length)
            stack.callback(stager.close)
            fd = open_corpus(args.corpus, layout, spec, args.io)
            stack.callback(os.close, fd)
            loader = LocalSource(fd, stager, device, spec, layout)
            if args.scheme == "solid" and args.profile == "sparse-decode" and args.prefetch_pages:
                pool = stack.enter_context(ThreadPoolExecutor(max_workers=1))
        trials = [trial(args, spec, seed, trace, device, base, loader, pool, layout) for _ in range(args.repeat)]
    medians = {key: median(t["summary"][key] for t in trials) for key in trials[0]["summary"]}
    return {"status": "ok", "layout": layout, "summary": medians, "trials": trials}
