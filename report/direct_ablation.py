"""Summarize the matched Direct I/O ablation and render its result tables."""

import hashlib
import json
from itertools import product
from statistics import mean, median

from figures import HIGH_RELOAD_RANGE, LOW_RELOAD_RANGE
from prefix import p95


GAPS = {0: "同时到达", 25: "高回载量请求早到 25 ms"}
METHODS = {
    "fifo": "原生 FIFO（整请求）",
    "chunk-fifo": "16 MiB 分块 FIFO",
    "chunk-rr": "SuperLoad",
}
BUFFERS = {"pageable": "普通主机缓冲", "pinned": "CANN 锁页路径"}


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


def render_table(summary, gap):
    settings = summary["gaps"][gap]["settings"]
    groups = []
    for buffer, label in BUFFERS.items():
        rows = [f'<tr class="ablation-group"><th scope="rowgroup" colspan="4">{label}</th></tr>']
        for method, name in METHODS.items():
            row = settings[f"{buffer}/{method}"]["median"]
            low = f'{row["small_p95_ms"]:,.0f}'
            if method == "chunk-rr":
                low = f"<strong>{low}</strong>"
            rows.append(
                f'<tr><th scope="row">{name}</th><td>{low}</td>'
                f'<td>{row["large_p95_ms"]:,.0f}</td><td>{row["throughput_gib_s"]:.4f}</td></tr>'
            )
        groups.append("<tbody>" + "".join(rows) + "</tbody>")
    return (
        f'<table class="ablation-table"><caption>{GAPS[gap]}</caption><thead><tr>'
        f'<th scope="col">方法</th><th scope="col">{LOW_RELOAD_RANGE}<br>P95（ms）↓</th>'
        f'<th scope="col">{HIGH_RELOAD_RANGE}<br>P95（ms）↓</th>'
        '<th scope="col">吞吐<br>（GiB/s）↑</th></tr></thead>'
        + "".join(groups) + "</table>"
    )


def render(summary):
    settings = summary["gaps"][25]["settings"]
    reductions = []
    for buffer in BUFFERS:
        fifo = settings[f"{buffer}/chunk-fifo"]["median"]
        superload = settings[f"{buffer}/chunk-rr"]["median"]
        reductions.append(100 * (1 - superload["small_p95_ms"] / fifo["small_p95_ms"]))
    return (
        '<p class="ablation-takeaway">高回载量请求早到 25 ms 时，相比 16 MiB 分块 FIFO，'
        f'普通主机缓冲和 CANN 锁页路径下的低回载量组 P95 分别下降 '
        f'<strong>{reductions[0]:.1f}%</strong> 和 <strong>{reductions[1]:.1f}%</strong>。</p>'
        + render_table(summary, 25)
        + '<details class="ablation-details"><summary>查看同时到达的结果</summary>'
        + render_table(summary, 0) + '</details>'
    )
