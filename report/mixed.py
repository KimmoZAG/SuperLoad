"""Read and render the four-scheme mixed-workload comparison."""

import csv
import math
from statistics import median


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


def panels(runs):
    sections = []
    for condition, (caption, workload) in COMPARISONS.items():
        rows = []
        for scheme, label in SCHEMES.items():
            values = [runs[(*condition, scheme, seed)] for seed in SEEDS]
            cells = []
            for samples, digits in zip(zip(*values), (2, 2, 3)):
                span = f"各轮范围 {min(samples):.{digits}f}–{max(samples):.{digits}f}"
                cells.append(f'<td><abbr title="{span}">{median(samples):.{digits}f}</abbr></td>')
            rows.append(f'<tr><th scope="row">{label}</th>{"".join(cells)}</tr>')
        sections.append(
            f'<article class="profile"><div class="table-wrap" role="region" aria-label="{caption}" tabindex="0">'
            f'<table><caption>{caption}。{workload}</caption><thead><tr>'
            '<th scope="col">方案</th><th scope="col">前缀 P95（ms）</th>'
            '<th scope="col">稀疏 P95（ms）</th><th scope="col">逻辑吞吐（GiB/s）</th>'
            f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div></article>'
        )
    return "\n".join(sections)
