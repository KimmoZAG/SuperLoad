"""Validate paired measurements and render the method comparison."""

import hashlib
import json
import math
from collections import defaultdict
from itertools import product
from statistics import median

from figures import MODES, panel_title, save
from matplotlib import pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.ticker import LogLocator, NullFormatter, ScalarFormatter
from prefix import p95


METHODS = {
    "fifo": ("FIFO", "#505963"),
    "bidaw": ("Bidaw", "#0072B2"),
    "strata-io": ("Strata I/O", "#008463"),
    "libra": ("Libra pipeline", "#C34E00"),
    "chunk-rr": ("Chunk RR", "#A44783"),
}
GAPS = {0: "同时到达", 25: "大回载早到 25 ms"}


def read(directory):
    payload = (directory / "plan.json").read_bytes()
    plan = json.loads(payload)
    digest = hashlib.sha256(payload).hexdigest()
    if plan["schema"] != 1 or plan["workload"] != "tool-agent-comparison" or not plan["pairs"]:
        raise ValueError("invalid comparison plan")

    measurements = {}
    for mode in MODES:
        run = json.loads((directory / f"{mode}.json").read_bytes())
        identity = (run["schema"], run["backend"], run["profile"], run["io"], run["storage"], run["plan_sha256"])
        if identity != (1, "ascend", "tool-agent-comparison", mode, "longhorn-ext4", digest):
            raise ValueError(f"wrong comparison identity or plan hash for {mode}")
        selected = run["pairs"]
        if (not selected or selected != sorted(set(selected))
                or not 0 <= selected[0] <= selected[-1] < len(plan["pairs"])):
            raise ValueError(f"invalid pair indices for {mode}")
        if not run["trials"] or run["chunk_blocks"] <= 0:
            raise ValueError(f"empty trials or invalid chunk size for {mode}")
        expected = set(product(selected, GAPS, METHODS))
        for trial_idx, trial in enumerate(run["trials"]):
            actual = {(row["pair"], row["gap_ms"], row["method"]) for row in trial}
            if len(trial) != len(expected) or actual != expected:
                raise ValueError(f"incomplete matched conditions for {mode} trial {trial_idx + 1}")
            for row in trial:
                pair = plan["pairs"][row["pair"]]
                verified = sum(min(2, len(pair[role])) for role in ("small", "large"))
                if row["verified_blocks"] not in (0, verified) or (trial_idx == 0 and row["verified_blocks"] == 0):
                    raise ValueError(f"incomplete HBM verification for {mode}/{row['method']}")
                if set(row["requests"]) != {"small", "large"} or row["elapsed_ms"] <= 0:
                    raise ValueError(f"incomplete pair for {mode}/{row['method']}")
                for role in ("small", "large"):
                    item = row["requests"][role]
                    blocks = len(pair[role])
                    amount = blocks * plan["page_bytes"]
                    if item["pages"] != blocks or item["storage_bytes"] != amount or item["hbm_bytes"] != amount:
                        raise ValueError(f"mismatched storage/HBM bytes for {mode}/{row['method']}/{role}")
                    metrics = [item[k] for k in ("queue_ms", "ready_ms", "service_ms", "disk_ns", "h2d_ns")]
                    if not all(math.isfinite(value) and value >= 0 for value in metrics):
                        raise ValueError(f"invalid latency for {mode}/{row['method']}/{role}")
                    if item["ready_ms"] + 1e-6 < item["queue_ms"] + item["service_ms"]:
                        raise ValueError(f"inconsistent latency for {mode}/{row['method']}/{role}")
        measurements[mode] = run
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
    render_layout(summary, output, mobile=False)
    render_layout(summary, output, mobile=True)


def render_layout(summary, output, *, mobile):
    modes = summary["measurements"]
    columns = (("small_p95", "Small P95"), ("large_p95", "Large P95"), ("gib_s", "Throughput"))
    for gap in GAPS:
        if mobile:
            fig, axes = plt.subplots(6, 1, figsize=(5.2, 15.5))
            fig.subplots_adjust(left=0.29, right=0.96, top=0.96, bottom=0.085, hspace=1.05)
        else:
            fig, axes = plt.subplots(2, 3, figsize=(11.4, 5.75), sharex="col", sharey=True)
            fig.subplots_adjust(left=0.135, right=0.982, top=0.90, bottom=0.19, hspace=0.42, wspace=0.29)
        for col, (metric, label) in enumerate(columns):
            points = [v for mode in modes.values() for m in METHODS
                      for v in mode["gaps"][gap][m][metric]["trials"]]
            latency = metric != "gib_s"
            for i, mode in enumerate(("buffered", "direct")):
                index = 2 * col + i if mobile else 3 * i + col
                ax = axes.flat[index]
                panel_title(ax, "ABCDEF"[index], f"{MODES[mode]} · {label}")
                if latency:
                    ax.set_xscale("log")
                    ax.set_xlim(min(points) * 0.8, max(points) * 1.2)
                    ax.xaxis.set_major_locator(LogLocator(base=10, subs=(1, 3)))
                    ax.xaxis.set_major_formatter(ScalarFormatter())
                    ax.xaxis.set_minor_formatter(NullFormatter())
                else:
                    ax.set_xlim(0, max(points) * 1.10)
                if i == 1 or mobile:
                    ax.set_xlabel("KV-ready latency (ms, log)" if latency else "Pair throughput (GiB/s)")
                ax.set_yticks(range(len(METHODS)), [name for name, _ in METHODS.values()])
                ax.set_ylim(len(METHODS) - 0.5, -0.5)
                ax.grid(axis="x")
                for j, (method, (_, color)) in enumerate(METHODS.items()):
                    stats = modes[mode]["gaps"][gap][method][metric]
                    values = stats["trials"]
                    ax.hlines(j, min(values), max(values), color=color, linewidth=1.4, alpha=0.65)
                    offsets = [j + (n - (len(values) - 1) / 2) * 0.19 for n in range(len(values))]
                    ax.scatter(values, offsets, facecolors="white", edgecolors=color, s=24, zorder=3)
                    ax.scatter(stats["median"], j, color=color, marker="D", s=28, zorder=4)
        legend = [
            Line2D([], [], marker="D", color="#4d5c64", ls="none", label="Median across trials"),
            Line2D([], [], marker="o", markerfacecolor="white", color="#4d5c64", ls="none",
                   label="Individual trial"),
        ]
        fig.legend(handles=legend, loc="lower center", bbox_to_anchor=(0.53, 0.025), ncol=2, frameon=False)
        suffix = "-mobile" if mobile else ""
        save(fig, output, f"method-comparison-{gap}ms{suffix}", pdf=not mobile)
