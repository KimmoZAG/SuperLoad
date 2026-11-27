"""Load and display matched trials of the mixed workload."""

import csv
import html
import json
import math
import tarfile
from pathlib import PurePosixPath
from statistics import median

from matplotlib.ticker import FormatStrFormatter, FuncFormatter, LogLocator, MultipleLocator

from figures import MODES, SCHEMES as FIGURE_SCHEMES, metric_panel, plt, save


SCHEMES = {
    "native-lru": "LRU",
    "strata": "Strata",
    "bidaw": "Bidaw",
    "solid": "SolidAttention",
}
COMPARISONS = {
    ("buffered", "mix-balanced"): ("常规混跑 · Buffered", "每轮 24 个前缀任务和 18 个稀疏任务。"),
    ("direct", "mix-balanced"): ("常规混跑 · Direct I/O", "每轮 24 个前缀任务和 18 个稀疏任务。"),
    ("buffered", "mix-sparse-heavy"): ("稀疏占优 · Buffered", "每轮 12 个前缀任务和 24 个稀疏任务。"),
}
METRICS = ("prefix_p95_ms", "sparse_p95_ms", "logical_gib_s")
SEEDS = (42, 43, 44)
JOINT_SCHEMES = {**SCHEMES, "joint": "联合切分"}
JOINT_STYLE = {
    "native-lru": ("LRU", "#505963", "-"),
    **{name: FIGURE_SCHEMES[name] for name in ("strata", "bidaw", "solid")},
    "joint": ("Joint scheduling", "#5441A4", "-"),
}
JOINT_RATES = {"buffered": 0.16, "direct": 0.10}
JOINT_COMPARISONS = {
    (mode, "mix-balanced"): (MODES[mode], f"名义输入速率 {rate:.2f} GiB/s。")
    for mode, rate in JOINT_RATES.items()
}
CHARTS = (
    ("prefix", "前缀回载 P95", "P95 KV-ready latency (ms, log scale)", 1),
    ("sparse", "稀疏回载 P95", "P95 KV-ready latency (ms, log scale)", 1),
    ("throughput", "逻辑吞吐", "Logical throughput (GiB/s)", 3),
)


def load(path):
    expected = {
        (*condition, scheme, seed)
        for condition in COMPARISONS for scheme in SCHEMES for seed in SEEDS
    }
    runs = {}
    with path.open(encoding="utf-8", newline="") as source:
        for row in csv.DictReader(source):
            key = (row["io"], row["scenario"], row["scheme"], int(row["seed"]))
            values = tuple(float(row[field]) for field in METRICS)
            if key not in expected or key in runs or not all(math.isfinite(v) and v > 0 for v in values):
                raise ValueError(f"invalid mixed-workload measurement {key} in {path}")
            runs[key] = values
    if expected != runs.keys():
        raise ValueError(f"missing mixed-workload measurements in {path}")
    return runs


def panels(runs, comparisons=COMPARISONS, schemes=SCHEMES):
    sections = []
    for condition, (caption, workload) in comparisons.items():
        rows = []
        for scheme, label in schemes.items():
            values = [runs[(*condition, scheme, seed)] for seed in SEEDS]
            cells = []
            for samples, digits in zip(zip(*values), (2, 2, 3)):
                span = f"各轮范围 {min(samples):.{digits}f}–{max(samples):.{digits}f}"
                cells.append(f'<td><abbr title="{span}">{median(samples):.{digits}f}</abbr></td>')
            rows.append(f'<tr><th scope="row">{label}</th>{"".join(cells)}</tr>')
        sections.append(
            f'<article class="profile"><div class="table-wrap mixed-table" role="region" aria-label="{caption}" tabindex="0">'
            f'<table><caption>{caption}。{workload}</caption><thead><tr>'
            '<th scope="col">方案</th><th scope="col">前缀 P95（ms）</th>'
            '<th scope="col">稀疏 P95（ms）</th><th scope="col">逻辑吞吐（GiB/s）</th>'
            f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div></article>'
        )
    return "\n".join(sections)


def load_joint(path):
    expected = {
        (*condition, scheme, seed)
        for condition in JOINT_COMPARISONS for scheme in JOINT_SCHEMES for seed in SEEDS
    }
    runs = {}
    with tarfile.open(path, "r:gz") as source:
        for member in source:
            if not member.isfile() or not member.name.endswith(".json"):
                continue
            mode = PurePosixPath(member.name).parent.name
            with source.extractfile(member) as file:
                trial = json.load(file)
            key = (mode, trial["scenario"], trial["scheme"], trial["seed"])
            values = (
                trial["classes"]["interactive-prefix"]["p95_ms"],
                trial["classes"]["sparse-decode"]["p95_ms"],
                trial["ready_gib_s"],
            )
            if (
                key not in expected or key in runs or trial["io"] != mode
                or trial["backend"] != "ascend"
                or trial["nominal_gib_s"] != JOINT_RATES[mode]
                or not all(math.isfinite(v) and v > 0 for v in values)
            ):
                raise ValueError(f"invalid joint-scheduling measurement {key} in {path}")
            runs[key] = values
    if expected != runs.keys():
        raise ValueError(f"missing joint-scheduling measurements in {path}")
    return runs


def write_joint_csv(runs, path):
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(("io", "scenario", "scheme", "seed", *METRICS))
        for key, values in sorted(runs.items()):
            writer.writerow((*key, *values))


def render_joint(runs, output):
    matrix = {}
    for mode, scenario in JOINT_COMPARISONS:
        for scheme in JOINT_SCHEMES:
            values = [runs[mode, scenario, scheme, seed] for seed in SEEDS]
            matrix[mode, scenario, scheme] = {
                "summary": dict(zip(METRICS, map(median, zip(*values)))),
                "trials": [{"summary": dict(zip(METRICS, row))} for row in values],
            }

    for index, ((name, _, xlabel, digits), metric) in enumerate(zip(CHARTS, METRICS)):
        values = [row[index] for row in runs.values()]
        limits = (min(values) * 0.82, max(values) * 2.3) if index < 2 else (0, max(values) * 1.30)
        for mode, scenario in JOINT_COMPARISONS:
            fig, ax = plt.subplots(figsize=(4.8, 2.65))
            fig.subplots_adjust(left=0.29, right=0.965, top=0.96, bottom=0.22)
            metric_panel(ax, matrix, mode, scenario, metric, limits, schemes=JOINT_STYLE, digits=digits)
            if metric.endswith("p95_ms"):
                ax.xaxis.set_major_locator(LogLocator(base=10, subs=(1, 3)))
                ax.xaxis.set_major_formatter(FuncFormatter(
                    lambda value, _: f"{value / 1000:g}k" if value >= 1000 else f"{value:g}"
                ))
            else:
                ax.xaxis.set_major_locator(MultipleLocator(0.05))
                ax.xaxis.set_major_formatter(FormatStrFormatter("%.2f"))
            ax.tick_params(axis="both", labelsize=10.5)
            ax.set_xlabel(xlabel, fontsize=10.5)
            ax.get_yticklabels()[-1].set_fontweight("semibold")
            ax.get_yticklabels()[-1].set_color(JOINT_STYLE["joint"][1])
            save(fig, output, f"mixed-{name}-{mode}", pdf=False)


def joint_dashboard(runs):
    sections = []
    for name, title, _, _ in CHARTS:
        panels_html = []
        for mode in JOINT_RATES:
            label = MODES[mode]
            rate = f"<span>输入 {JOINT_RATES[mode]:.2f} GiB/s</span>" if name == "prefix" else ""
            alt = html.escape(f"{label} 的五方案{title}。联合切分在图中标为 Joint scheduling。")
            panels_html.append(
                '<figure class="mixed-chart">'
                f'<h4>{label}{rate}</h4>'
                f'<img src="figures/mixed-{name}-{mode}.svg" alt="{alt}" decoding="async">'
                '</figure>'
            )
        sections.append(
            f'<section class="mixed-metric" aria-labelledby="mixed-{name}-title">'
            f'<h3 id="mixed-{name}-title">{title}</h3>'
            f'<div class="mixed-chart-grid">{"".join(panels_html)}</div></section>'
        )
    return (
        '<div class="mixed-charts">'
        '<div class="mixed-legend" aria-label="图例">'
        '<span><b aria-hidden="true">◆</b> 中位数</span>'
        '<span><b aria-hidden="true">○</b> 单轮</span>'
        '<span><b aria-hidden="true">━</b> 延迟范围</span>'
        '<span class="mixed-joint-label">联合切分（Joint scheduling）</span></div>'
        + "".join(sections)
        + '</div><details class="mixed-details"><summary>数值表</summary>'
        + panels(runs, JOINT_COMPARISONS, JOINT_SCHEMES)
        + '</details>'
    )
