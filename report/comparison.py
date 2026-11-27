"""Validate paired measurements and render the method comparison."""

import hashlib
import json
import math
from collections import defaultdict
from itertools import product
from statistics import median

from figures import MODES, render_reload_panels
from prefix import p95


METHODS = {
    "fifo": ("FIFO", "#505963"),
    "bidaw": ("Bidaw", "#0072B2"),
    "strata-io": ("Strata I/O", "#008463"),
    "libra": ("Libra pipeline", "#C34E00"),
    "chunk-rr": ("SuperLoad", "#A44783"),
}
GAPS = {0: "同时到达", 25: "高回载量请求早到 25 ms"}
GROUP_LABELS = {**MODES, "buffered-pinned": "Buffered pinned"}
BUFFERED_MODES = {"pageable": "buffered", "pinned": "buffered-pinned"}


def split_host_buffers(mode, run):
    if run["schema"] == 1:
        return {mode: run}
    if mode != "buffered" or run["schema"] != 2 or run["host_buffers"] != list(BUFFERED_MODES):
        raise ValueError(f"unexpected host buffer configuration for {mode}")
    if any(row.get("host_buffer") not in BUFFERED_MODES for trial in run["trials"] for row in trial):
        raise ValueError("unknown host buffer in paired Buffered I/O results")
    return {
        name: {**run, "trials": [[row for row in trial if row["host_buffer"] == buffer]
                                 for trial in run["trials"]]}
        for buffer, name in BUFFERED_MODES.items()
    }


def read(directory):
    payload = (directory / "plan.json").read_bytes()
    plan = json.loads(payload)
    digest = hashlib.sha256(payload).hexdigest()
    if plan["schema"] != 1 or plan["workload"] != "tool-agent-comparison" or not plan["pairs"]:
        raise ValueError("invalid comparison plan")

    measurements = {}
    for mode in MODES:
        run = json.loads((directory / f"{mode}.json").read_bytes())
        identity = (run["backend"], run["profile"], run["io"], run["storage"], run["plan_sha256"])
        if identity != ("ascend", "tool-agent-comparison", mode, "longhorn-ext4", digest):
            raise ValueError(f"wrong comparison identity or plan hash for {mode}")
        for name, run in split_host_buffers(mode, run).items():
            selected = run["pairs"]
            if (not selected or selected != sorted(set(selected))
                    or not 0 <= selected[0] <= selected[-1] < len(plan["pairs"])):
                raise ValueError(f"invalid pair indices for {name}")
            if not run["trials"] or run["chunk_blocks"] <= 0:
                raise ValueError(f"empty trials or invalid chunk size for {name}")
            expected = set(product(selected, GAPS, METHODS))
            for trial_idx, trial in enumerate(run["trials"]):
                actual = {(row["pair"], row["gap_ms"], row["method"]) for row in trial}
                if len(trial) != len(expected) or actual != expected:
                    raise ValueError(f"incomplete matched conditions for {name} trial {trial_idx + 1}")
                for row in trial:
                    pair = plan["pairs"][row["pair"]]
                    verified = sum(min(2, len(pair[role])) for role in ("small", "large"))
                    if row["verified_blocks"] not in (0, verified) or (trial_idx == 0 and row["verified_blocks"] == 0):
                        raise ValueError(f"incomplete HBM verification for {name}/{row['method']}")
                    if set(row["requests"]) != {"small", "large"} or row["elapsed_ms"] <= 0:
                        raise ValueError(f"incomplete pair for {name}/{row['method']}")
                    for role in ("small", "large"):
                        item = row["requests"][role]
                        blocks = len(pair[role])
                        amount = blocks * plan["page_bytes"]
                        if item["pages"] != blocks or item["storage_bytes"] != amount or item["hbm_bytes"] != amount:
                            raise ValueError(f"mismatched storage/HBM bytes for {name}/{row['method']}/{role}")
                        metrics = [item[k] for k in ("queue_ms", "ready_ms", "service_ms", "disk_ns", "h2d_ns")]
                        if not all(math.isfinite(value) and value >= 0 for value in metrics):
                            raise ValueError(f"invalid latency for {name}/{row['method']}/{role}")
                        if item["ready_ms"] + 1e-6 < item["queue_ms"] + item["service_ms"]:
                            raise ValueError(f"inconsistent latency for {name}/{row['method']}/{role}")
            measurements[name] = run
    return plan, measurements


def summarize(plan, runs):
    result = {key: plan[key] for key in (
        "trace_sha256", "capacity_gib", "disjoint_windows", "shared_storage_windows", "page_bytes",
    )}
    result["measurements"] = {}
    for mode, run in runs.items():
        values = defaultdict(lambda: defaultdict(list))
        for trial in run["trials"]:
            indexed = {(row["gap_ms"], row["method"], row["pair"]): row for row in trial}
            for gap, method in product(GAPS, METHODS):
                rows = [indexed[gap, method, i] for i in run["pairs"]]
                small = [row["requests"]["small"] for row in rows]
                large = [row["requests"]["large"] for row in rows]
                fifo = [indexed[gap, "fifo", i]["requests"]["small"] for i in run["pairs"]]
                metrics = values[gap, method]
                metrics["small_p95"].append(p95([r["ready_ms"] for r in small]))
                metrics["large_p95"].append(p95([r["ready_ms"] for r in large]))
                metrics["small_queue_p95"].append(p95([r["queue_ms"] for r in small]))
                metrics["paired_small_delta_ms"].append(median(
                    r["ready_ms"] - control["ready_ms"] for r, control in zip(small, fifo)
                ))
                total_gib = sum(r["hbm_bytes"] for r in small + large) / (1 << 30)
                metrics["gib_s"].append(total_gib / (sum(row["elapsed_ms"] for row in rows) / 1000))
                metrics["read_calls"].append(sum(r["read_calls"] for r in small + large) / len(rows))
                metrics["large_overlap_ms"].append(median(r.get("pipeline_overlap_ns", 0) for r in large) / 1e6)
        result["measurements"][mode] = {
            "pairs": len(run["pairs"]), "repeat": len(run["trials"]), "chunk_blocks": run["chunk_blocks"],
            "gaps": {
                gap: {
                    method: {key: {"median": median(samples), "trials": samples}
                             for key, samples in values[gap, method].items()}
                    for method in METHODS
                }
                for gap in GAPS
            },
        }
    return result


def render(summary, output):
    for gap in GAPS:
        measurements = summary["measurements"]
        groups = {label: measurements[mode]["gaps"][gap]
                  for mode, label in GROUP_LABELS.items() if mode in measurements}
        for mobile in (False, True):
            render_reload_panels(groups, METHODS, output, f"method-comparison-{gap}ms", mobile=mobile)
