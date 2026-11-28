"""Render the validated method comparison."""

import html

from comparison_data import compare_with_controls, read, summarize
from figures import RELOAD_METRICS, render_reload_panels


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


def render(summary, output):
    for gap in GAPS:
        measurements = summary["measurements"]
        groups = {label: measurements[mode]["gaps"][gap]
                  for mode, label in GROUP_LABELS.items() if mode in measurements}
        for mobile in (False, True):
            render_reload_panels(groups, METHODS, output, f"method-comparison-{gap}ms", mobile=mobile)


def render_finding(summary):
    scores = compare_with_controls(summary)
    wins = sum(score["wins"] for score in scores)
    return (
        '<div class="comparison-finding">'
        f'<strong>{wins}/{len(scores)} 项</strong>'
        '<p><b>优化后 SuperLoad 领先最强对照</b>'
        '<span>在相同的 I/O 配置和到达条件下，逐项比较两组 HBM 就绪 P95 和双请求吞吐。</span></p></div>'
    )


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
