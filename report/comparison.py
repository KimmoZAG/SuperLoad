"""Validate paired measurements and render the method comparison."""

import hashlib
import html
import json
import math
from collections import defaultdict
from itertools import product
from statistics import median

from figures import RELOAD_METRICS, render_reload_panels
from prefix import p95


METHODS = {
    "fifo": ("FIFO", "#525C67"),
    "bidaw": ("Bidaw", "#0072B2"),
    "strata-io": ("Strata I/O", "#66702E"),
    "libra": ("Libra pipeline", "#C34E00"),
    "chunk-rr": ("SuperLoad (orig.)", "#A44783"),
    "superload": ("SuperLoad (opt.)", "#006C59"),
}
GAPS = {0: "同时到达", 25: "高回载量请求早到 25 ms"}
GROUP_LABELS = {"buffered": "Buffered · pageable", "buffered-pinned": "Buffered · pinned", "direct": "Direct I/O"}
GROUP_TITLES = {"buffered": "Buffered I/O · 普通缓冲", "buffered-pinned": "Buffered I/O · CANN 锁页缓冲",
                "direct": "Direct I/O · 普通缓冲"}
METHOD_TITLES = {"fifo": "原生 FIFO", "bidaw": "Bidaw", "strata-io": "Strata I/O", "libra": "Libra",
                 "chunk-rr": "原 SuperLoad", "superload": "优化后 SuperLoad"}


def read(directory):
    payload = (directory / "plan.json").read_bytes()
    plan = json.loads(payload)
    digest = hashlib.sha256(payload).hexdigest()
    if plan["schema"] != 1 or plan["workload"] != "tool-agent-comparison" or not plan["pairs"]:
        raise ValueError("invalid comparison plan")

    measurements = {}
    buffered = json.loads((directory / "buffered.json").read_bytes())
    for name in GROUP_LABELS:
        mode = "direct" if name == "direct" else "buffered"
        host = "pinned" if name == "buffered-pinned" else "pageable"
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
        expected = set(product(selected, GAPS, METHODS))
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
            "storage": run["storage"], "superload_memory": run["superload_memory"][run["host_buffers"][0]],
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


def render_tables(summary):
    units = {"small_p95": "ms", "large_p95": "ms", "gib_s": "GiB/s"}
    headers = "".join(f'<th scope="col">{html.escape(title)}</th>' for title in METHOD_TITLES.values())
    tables = []
    for mode, measurement in summary["measurements"].items():
        rows = []
        for gap, label in GAPS.items():
            for metric, title in RELOAD_METRICS:
                values = [measurement["gaps"][gap][method][metric]["median"] for method in METHODS]
                best = (max if metric == "gib_s" else min)(values)
                digits = 3 if metric == "gib_s" else 2
                cells = []
                for value in values:
                    formatted = f"{value:.{digits}f}"
                    cells.append(f"<td>{formatted}</td>" if value != best else f"<td><strong>{formatted}</strong></td>")
                measure_name = "双请求吞吐" if metric == "gib_s" else title
                rows.append(f'<tr><td>{label}</td><th scope="row">{measure_name}（{units[metric]}）</th>{"".join(cells)}</tr>')
        name = GROUP_TITLES[mode]
        tables.append(
            f'<div class="table-scroll" tabindex="0" role="region" aria-label="{name}实测表"><table>'
            f'<caption>{name}</caption><thead><tr><th scope="col">到达条件</th>'
            f'<th scope="col">指标</th>{headers}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
        )
    return "".join(tables)
