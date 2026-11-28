"""Validate and summarize paired comparison measurements without plotting dependencies."""

import hashlib
import json
import math
from collections import defaultdict
from itertools import product
from statistics import median


METHODS = ("fifo", "bidaw", "strata-io", "libra", "chunk-rr", "superload")
GAPS_MS = (0, 25)
PROFILES = {
    "buffered": ("buffered", "pageable"),
    "buffered-pinned": ("buffered", "pinned"),
    "direct": ("direct", "pageable"),
}


def p95(values):
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def read(directory):
    payload = (directory / "plan.json").read_bytes()
    plan = json.loads(payload)
    digest = hashlib.sha256(payload).hexdigest()
    if plan["schema"] != 1 or plan["workload"] != "tool-agent-comparison" or not plan["pairs"]:
        raise ValueError("invalid comparison plan")

    measurements = {}
    buffered = json.loads((directory / "buffered.json").read_bytes())
    for name, (mode, host) in PROFILES.items():
        separate = directory / f"{name}.json"
        if name == "buffered" or (name == "buffered-pinned" and not separate.exists()):
            run = buffered
        else:
            run = json.loads(separate.read_bytes())
        identity = (run["schema"], run["backend"], run["profile"], run["io"], run["plan_sha256"])
        if identity != (3, "ascend", "tool-agent-comparison", mode, digest):
            raise ValueError(f"wrong comparison identity or plan hash for {name}")
        if mode == "buffered" and run["host_buffers"] == ["pageable", "pinned"]:
            run = {**run, "host_buffers": [host],
                   "trials": [[row for row in trial if row["host_buffer"] == host] for trial in run["trials"]]}
        if run["host_buffers"] != [host]:
            raise ValueError(f"unexpected host buffer configuration for {name}")
        selected = run["pairs"]
        if (not selected or selected != sorted(set(selected))
                or not 0 <= selected[0] <= selected[-1] < len(plan["pairs"])):
            raise ValueError(f"invalid pair indices for {name}")
        if not run["trials"] or run["chunk_blocks"] <= 0:
            raise ValueError(f"empty trials or invalid chunk size for {name}")
        expected = set(product(selected, GAPS_MS, METHODS))
        for trial_idx, trial in enumerate(run["trials"]):
            actual = {(row["pair"], row["gap_ms"], row["method"]) for row in trial}
            if len(trial) != len(expected) or actual != expected:
                raise ValueError(f"incomplete matched conditions for {name} trial {trial_idx + 1}")
            for row in trial:
                pair = plan["pairs"][row["pair"]]
                verified = sum(min(2, len(pair[role])) for role in ("small", "large"))
                if row["host_buffer"] != host or row["verified_blocks"] != verified:
                    raise ValueError(f"incomplete HBM verification or wrong buffer for {name}/{row['method']}")
                if set(row["requests"]) != {"small", "large"} or not 0 < row["elapsed_ms"] < math.inf:
                    raise ValueError(f"incomplete pair for {name}/{row['method']}")
                for role, item in row["requests"].items():
                    blocks = len(pair[role])
                    amount = blocks * plan["page_bytes"]
                    if item["pages"] != blocks or item["storage_bytes"] != amount or item["hbm_bytes"] != amount:
                        raise ValueError(f"mismatched storage/HBM bytes for {name}/{row['method']}/{role}")
                    times = [item[k] for k in ("queue_ms", "ready_ms", "service_ms", "disk_ns", "h2d_ns")]
                    if not all(math.isfinite(value) and value >= 0 for value in times):
                        raise ValueError(f"invalid latency for {name}/{row['method']}/{role}")
                    if item["ready_ms"] + 1e-6 < item["queue_ms"] + item["service_ms"]:
                        raise ValueError(f"inconsistent latency for {name}/{row['method']}/{role}")
        measurements[name] = run
    if len({(tuple(run["pairs"]), len(run["trials"]), run["storage"]) for run in measurements.values()}) != 1:
        raise ValueError("comparison profiles must use the same pairs, repetitions and storage")
    return plan, measurements


def summarize(plan, runs):
    result = {key: plan[key] for key in ("trace_sha256", "page_bytes")}
    result["measurements"] = {}
    for mode, run in runs.items():
        values = defaultdict(lambda: defaultdict(list))
        for trial in run["trials"]:
            indexed = {(row["gap_ms"], row["method"], row["pair"]): row for row in trial}
            for gap, method in product(GAPS_MS, METHODS):
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
            "storage": run["storage"], "superload_memory": run["superload_memory"][run["host_buffers"][0]],
            "gaps": {
                gap: {
                    method: {key: {"median": median(samples), "trials": samples}
                             for key, samples in values[gap, method].items()}
                    for method in METHODS
                }
                for gap in GAPS_MS
            },
        }
    return result


def compare_with_controls(summary):
    """Compare each metric with the strongest control under the same conditions."""
    scores = []
    for profile, measured in summary["measurements"].items():
        for gap, methods in measured["gaps"].items():
            for metric, best in (("small_p95", min), ("large_p95", min), ("gib_s", max)):
                control, value = best(
                    ((name, row[metric]["median"]) for name, row in methods.items() if name != "superload"),
                    key=lambda item: item[1],
                )
                candidate = methods["superload"][metric]["median"]
                scores.append({"profile": profile, "gap_ms": gap, "metric": metric, "control": control,
                               "control_value": value, "superload": candidate,
                               "wins": candidate != value and best(candidate, value) == candidate})
    return scores
