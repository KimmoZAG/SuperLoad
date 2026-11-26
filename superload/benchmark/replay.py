"""Schedule requests, measure KV readiness, and check replayed pages."""

import time
from collections import Counter

from ..dataset.corpus import interleave, page_bytes
from ..schemes.bidaw import HostCache
from .metrics import summarize


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
        if cache is not None:
            cache.finish_wave(requests)
    metrics["logical_pages"] = sum(row["pages"] for row in rows)
    metrics["active_ns"] = done - start
    return metrics, rows, snapshot


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
