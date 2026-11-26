"""Latency percentiles, throughput, and data movement statistics."""


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
