"""Validate and present the Agent prefix contention study."""

import hashlib
import json
import math
from collections import defaultdict
from statistics import median

from matplotlib import pyplot as plt
from matplotlib.ticker import LogLocator, NullFormatter, PercentFormatter, ScalarFormatter

from comparison_data import p95
from figures import HIGH_RELOAD_RANGE, LOW_RELOAD_RANGE, MODES, panel_title, save


CASES = {
    "small-only": (f"{LOW_RELOAD_RANGE} alone", "#505963", "-"),
    "small-first": (f"{LOW_RELOAD_RANGE} first", "#0072B2", "--"),
    "large-first": (f"{HIGH_RELOAD_RANGE} first", "#C34E00", "-"),
    "large-first-25ms": (f"{HIGH_RELOAD_RANGE} 25 ms earlier", "#008463", "-."),
}


def read(directory):
    payload = (directory / "plan.json").read_bytes()
    plan = json.loads(payload)
    digest = hashlib.sha256(payload).hexdigest()
    pairs = plan["pairs"]
    measurements = {}
    for mode in MODES:
        run = json.loads((directory / f"{mode}.json").read_bytes())
        if (run["schema"], run["backend"], run["profile"], run["io"], run["plan_sha256"]) != (
            1, "ascend", "tool-agent-prefix", mode, digest,
        ):
            raise ValueError(f"unexpected prefix measurement identity or plan hash for {mode}")
        if run["storage"] != "longhorn-ext4" or not run["trials"]:
            raise ValueError(f"unexpected prefix storage or empty trials for {mode}")
        selected = run["pairs"]
        if not selected or selected != sorted(set(selected)) or not 0 <= selected[0] <= selected[-1] < len(pairs):
            raise ValueError(f"invalid prefix pair indices for {mode}")
        for trial in run["trials"]:
            if sorted(row["pair"] for row in trial) != selected:
                raise ValueError(f"incomplete prefix pair coverage for {mode}")
            for row in trial:
                pair = pairs[row["pair"]]
                expected_checks = min(2, len(pair["small"])) + min(2, len(pair["large"]))
                if row["verified_blocks"] != expected_checks or set(row["cases"]) != set(CASES):
                    raise ValueError(f"incomplete prefix controls or HBM checks for {mode}")
                for case, sample in row["cases"].items():
                    roles = {"small"} if case == "small-only" else {"small", "large"}
                    if set(sample["requests"]) != roles or sample["elapsed_ms"] <= 0:
                        raise ValueError(f"incomplete role coverage for {mode}/{case}")
                    for role in roles:
                        stats = sample["requests"][role]
                        amount = len(pair[role]) * plan["page_bytes"]
                        if stats["hbm_bytes"] != amount or stats["storage_bytes"] != amount:
                            raise ValueError(f"mismatched prefix bytes for {mode}/{case}/{role}")
                        if not all(math.isfinite(stats[key]) and stats[key] >= 0
                                   for key in ("ready_ms", "queue_ms", "service_ms")):
                            raise ValueError(f"invalid prefix latency for {mode}/{case}/{role}")
                        if not math.isclose(stats["ready_ms"], stats["queue_ms"] + stats["service_ms"]):
                            raise ValueError(f"inconsistent prefix timing for {mode}/{case}/{role}")
        measurements[mode] = run
    return plan, measurements


def summarize(plan, measurements):
    sizes = [len(p["large"]) * plan["page_bytes"] / (1 << 20) for p in plan["pairs"]]
    summary = {
        "requests": plan["requests"], "source_windows": plan["source_windows"],
        "warmup_windows": plan["warmup_windows"], "page_bytes": plan["page_bytes"],
        "primary_gib": plan["primary_gib"], "primary_order_seed": plan["primary_order_seed"],
        "sweep": plan["sweep"], "pairs": len(sizes), "large_mib": [min(sizes), median(sizes), max(sizes)],
        "measurements": {},
    }
    for mode, run in measurements.items():
        cases = {}
        for case in CASES:
            metrics = defaultdict(list)
            for trial in run["trials"]:
                observations = [r["cases"][case] for r in trial]
                small = [s["requests"]["small"] for s in observations]
                metrics["small_ready_p95_ms"].append(p95([r["ready_ms"] for r in small]))
                metrics["small_queue_p95_ms"].append(p95([r["queue_ms"] for r in small]))
                deltas = [
                    s["ready_ms"] - r["cases"]["small-only"]["requests"]["small"]["ready_ms"]
                    for s, r in zip(small, trial)
                ]
                metrics["paired_small_delta_median_ms"].append(median(deltas))
                if case != "small-only":
                    large = [s["requests"]["large"] for s in observations]
                    metrics["large_ready_p95_ms"].append(p95([r["ready_ms"] for r in large]))
                    gib = sum(r["hbm_bytes"] for r in small + large) / (1 << 30)
                    metrics["pair_gib_s"].append(gib / (sum(r["elapsed_ms"] for r in observations) / 1000))
            cases[case] = {key: {"median": median(values), "trials": values} for key, values in metrics.items()}
        samples = [r["cases"] for trial in run["trials"] for r in trial]
        large_first = [sample["large-first"]["requests"]["large"] for sample in samples]
        summary["measurements"][mode] = {
            "cases": cases, "repeat": len(run["trials"]), "pairs": len(run["pairs"]), "samples": len(samples),
            "small_delayed_by_large": sum(
                sample["large-first"]["requests"]["small"]["ready_ms"]
                > sample["small-only"]["requests"]["small"]["ready_ms"]
                for sample in samples
            ),
            "large_first_stage_median_ms": {
                "file_read": median(row["disk_ns"] for row in large_first) / 1e6,
                "h2d": median(row["h2d_ns"] for row in large_first) / 1e6,
            },
        }
    return summary


def render(summary, measurements, output):
    output.mkdir(parents=True, exist_ok=True)
    render_layout(summary, measurements, output, mobile=False)
    render_layout(summary, measurements, output, mobile=True)


def render_layout(summary, measurements, output, *, mobile):
    if mobile:
        fig, axes = plt.subplots(3, 1, figsize=(5.0, 9.6))
        fig.subplots_adjust(left=0.18, right=0.96, top=0.945, bottom=0.13, hspace=0.57)
    else:
        fig, axes = plt.subplots(1, 3, figsize=(11.8, 4.45))
        fig.subplots_adjust(left=0.095, right=0.98, top=0.85, bottom=0.22, wspace=0.32)
    sweep = defaultdict(list)
    for row in summary["sweep"]:
        sweep[tuple(row["capacity_gib"])].append(row["mixed_windows"] / row["windows"])
    ax = axes[0]
    panel_title(ax, "A", "Both reload groups in one window")
    for index, (capacity, fractions) in enumerate(sweep.items()):
        color = "#0072B2" if list(capacity) == summary["primary_gib"] else "#505963"
        ax.hlines(index, min(fractions), max(fractions), color=color, linewidth=3)
        ax.scatter(median(fractions), index, marker="D", color=color, s=23, zorder=3)
    ax.set_yticks(range(len(sweep)), ["/".join(map(str, key)) for key in sweep])
    ax.invert_yaxis()
    ax.set_ylabel("HBM / DRAM / storage (GiB)")
    ax.set_xlabel("Fraction of all windows")
    ax.set_xlim(0, max(max(values) for values in sweep.values()) * 1.08)
    ax.xaxis.set_major_formatter(PercentFormatter(1, decimals=0))
    ax.grid(axis="x")

    samples_by_mode = {}
    for mode in ("direct", "buffered"):
        samples_by_mode[mode] = {
            case: sorted(r["cases"][case]["requests"]["small"]["ready_ms"]
                         for trial in measurements[mode]["trials"] for r in trial)
            for case in CASES
        }
    all_samples = [x for cases in samples_by_mode.values() for values in cases.values() for x in values]
    for axis, letter, mode in zip(axes[1:], "BC", samples_by_mode):
        panel_title(axis, letter, MODES[mode])
        for case, (label, color, linestyle) in CASES.items():
            values = samples_by_mode[mode][case]
            axis.step(values, [(i + 1) / len(values) for i in range(len(values))],
                      where="post", label=label, color=color, linestyle=linestyle, linewidth=1.65)
        axis.axhline(0.95, color="#65747C", linestyle=":", linewidth=0.8)
        axis.set_xscale("log")
        axis.set_xlim(min(all_samples) * 0.8, max(all_samples) * 1.12)
        axis.xaxis.set_major_locator(LogLocator(base=10, subs=(1, 3)))
        axis.xaxis.set_major_formatter(ScalarFormatter())
        axis.xaxis.set_minor_formatter(NullFormatter())
        axis.set_ylim(0, 1.03)
        axis.set_yticks([0, 0.25, 0.5, 0.75, 0.95])
        axis.yaxis.set_major_formatter(PercentFormatter(1))
        axis.set_xlabel(f"{LOW_RELOAD_RANGE} KV-ready latency (ms, log)")
        axis.grid(axis="y")
    axes[1].set_ylabel("Empirical CDF")
    if mobile:
        axes[2].set_ylabel("Empirical CDF")
    fig.legend(*axes[1].get_legend_handles_labels(), loc="lower center",
               bbox_to_anchor=(0.55, 0.025 if mobile else 0.035),
               frameon=False, ncol=2 if mobile else 4, fontsize=9)
    name = "tool-agent-prefix-mobile" if mobile else "tool-agent-prefix"
    save(fig, output, name, pdf=not mobile)
