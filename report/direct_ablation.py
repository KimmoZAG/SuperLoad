"""Summarize the matched Direct I/O ablation and render its result figures."""

import hashlib
import json
from itertools import product
from statistics import mean, median

from comparison import GAPS, METHODS as COMPARISON_METHODS
from figures import render_reload_panels
from prefix import p95


METHODS = {
    "fifo": ("Native FIFO", COMPARISON_METHODS["fifo"][1]),
    "chunk-fifo": ("16 MiB chunk FIFO", "#0072B2"),
    "chunk-rr": COMPARISON_METHODS["chunk-rr"],
}
BUFFERS = {"pageable": "Pageable", "pinned": "CANN pinned"}


def read(directory):
    payload = (directory / "plan.json").read_bytes()
    plan = json.loads(payload)
    run = json.loads((directory / "direct.json").read_bytes())
    if (run["schema"], run["workload"], run["backend"], run["io"], run["storage"]) != (
        1, "superload-direct-fifo-ablation", "ascend", "direct", "longhorn-ext4",
    ):
        raise ValueError("unexpected Direct I/O ablation identity")
    if run["plan_sha256"] != hashlib.sha256(payload).hexdigest():
        raise ValueError("Direct I/O ablation plan differs from the measured plan")
    if (run["chunk_blocks"] != 2 or run["gaps_ms"] != list(GAPS)
            or run["methods"] != list(METHODS) or run["host_buffers"] != list(BUFFERS)):
        raise ValueError("unexpected Direct I/O ablation conditions")
    pairs = run["pairs"]
    if not pairs or pairs != sorted(set(pairs)) or not 0 <= pairs[0] <= pairs[-1] < len(plan["pairs"]):
        raise ValueError("invalid Direct I/O ablation pairs")
    expected = set(product(GAPS, METHODS, BUFFERS, pairs))
    if not run["trials"] or len(run["trials"]) != run["repeat"]:
        raise ValueError("incomplete Direct I/O ablation rounds")
    for trial in run["trials"]:
        actual = {(r["gap_ms"], r["method"], r["host_buffer"], r["pair"]) for r in trial}
        if len(trial) != len(expected) or actual != expected:
            raise ValueError("incomplete Direct I/O ablation conditions")
    return run, plan["page_bytes"]


def metrics(row):
    small, large = (row["requests"][role] for role in ("small", "large"))
    requests = (small, large)
    return {
        "small_ready_ms": small["ready_ms"],
        "large_ready_ms": large["ready_ms"],
        "small_first_queue_ms": small["queue_ms"],
        "large_inter_chunk_wait_ms": max(0, large["ready_ms"] - large["queue_ms"] - large["service_ms"]),
        "pair_elapsed_ms": row["elapsed_ms"],
        "h2d_ms": sum(r["h2d_ns"] for r in requests) / 1e6,
        "disk_ms": sum(r["disk_ns"] for r in requests) / 1e6,
        "cpu_staging_ms": sum(r.get("cpu_staging_ns", 0) for r in requests) / 1e6,
        "read_calls": sum(r["read_calls"] for r in requests),
        "hbm_bytes": sum(r["hbm_bytes"] for r in requests),
    }


def round_metrics(rows):
    return {
        "small_p95_ms": p95([r["small_ready_ms"] for r in rows]),
        "large_p95_ms": p95([r["large_ready_ms"] for r in rows]),
        "throughput_gib_s": (sum(r["hbm_bytes"] for r in rows) / (1 << 30)
                             / (sum(r["pair_elapsed_ms"] for r in rows) / 1000)),
        **{key: mean(r[key] for r in rows) for key in rows[0] if key != "hbm_bytes"},
    }


def paired_delta(indexes, pairs, gap, target, control):
    deltas = []
    for pair in pairs:
        samples = [(idx[(gap, *target, pair)], idx[(gap, *control, pair)]) for idx in indexes]
        deltas.append({key: mean(a[key] - b[key] for a, b in samples)
                       for key in samples[0][0] if key != "hbm_bytes"})
    return {
        "mean_delta": {key: mean(d[key] for d in deltas) for key in deltas[0]},
        "median_delta": {key: median(d[key] for d in deltas) for key in deltas[0]},
        "pairs_with_lower_small_ready_ms": sum(d["small_ready_ms"] < 0 for d in deltas),
    }


def summarize(run, page_bytes):
    pairs = run["pairs"]
    indexes = [
        {(r["gap_ms"], r["method"], r["host_buffer"], r["pair"]): metrics(r) for r in trial}
        for trial in run["trials"]
    ]
    result = {
        "revision": run["revision"], "pairs": pairs, "repeat": run["repeat"],
        "chunk_mib": run["chunk_blocks"] * page_bytes // (1 << 20),
        "cases": sum(map(len, indexes)), "gaps": {},
    }
    for gap in GAPS:
        settings = {}
        for buffer, method in product(BUFFERS, METHODS):
            trials = [round_metrics([idx[gap, method, buffer, pair] for pair in pairs]) for idx in indexes]
            settings[f"{buffer}/{method}"] = {
                "trials": trials,
                "median": {key: median(t[key] for t in trials) for key in trials[0]},
            }
        comparisons = {
            f"{target[0]}/{target[1]}-vs-{control[0]}/{control[1]}":
                paired_delta(indexes, pairs, gap, target, control)
            for target, control in (
                [((a, b), (c, b)) for b in BUFFERS
                 for a, c in (("chunk-fifo", "fifo"), ("chunk-rr", "chunk-fifo"))]
                + [((m, "pinned"), (m, "pageable")) for m in METHODS]
            )
        }
        result["gaps"][gap] = {"settings": settings, "paired_comparisons": comparisons}
    return result


def render(summary, output):
    metrics = {
        "small_p95": "small_p95_ms",
        "large_p95": "large_p95_ms",
        "gib_s": "throughput_gib_s",
    }
    for gap in GAPS:
        settings = summary["gaps"][gap]["settings"]
        groups = {}
        for buffer, label in BUFFERS.items():
            groups[label] = methods = {}
            for method in METHODS:
                row = settings[f"{buffer}/{method}"]
                methods[method] = {
                    metric: {"trials": [trial[key] for trial in row["trials"]], "median": row["median"][key]}
                    for metric, key in metrics.items()
                }
        for mobile in (False, True):
            render_reload_panels(
                groups, METHODS, output, f"direct-io-ablation-{gap}ms",
                mobile=mobile, log_metrics=("small_p95",),
            )
