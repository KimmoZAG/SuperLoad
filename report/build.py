"""Build the Agent prefix report from the pinned measurement Releases."""

import argparse
import html
import json
import shutil
from base64 import b64encode
from pathlib import Path

import comparison
import direct_ablation
from figures import HIGH_RELOAD_RANGE, LOW_RELOAD_RANGE
from overview import render_overview, render_poster_results
import prefix
from release_data import ResultsRelease


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
STATIC = HERE / "static"
RETIRED_ASSETS = ("report.js", "summary.json", "mixed-results.csv")
RETIRED_FIGURES = (
    "interactive-prefix-performance", "sparse-decode-performance",
    "latency-ecdf", "mechanism-ablation", "roce-stages",
)
RETIRED_MIXED_CHARTS = tuple(
    f"mixed-{metric}-{mode}.svg"
    for metric in ("prefix", "sparse", "throughput")
    for mode in ("buffered", "direct")
)


def svg_data_url(path):
    return "data:image/svg+xml;base64," + b64encode(path.read_bytes()).decode("ascii")


def figure(directory, name, alt):
    label = html.escape(alt)
    # Keep the report readable even when individual figure requests fail.
    desktop = svg_data_url(directory / f"{name}.svg")
    mobile = svg_data_url(directory / f"{name}-mobile.svg")
    return (
        '<figure class="paper-figure"><picture>'
        f'<source media="(max-width: 48rem)" srcset="{mobile}">'
        f'<img src="{desktop}" alt="{label}" decoding="async"></picture>'
        f'<figcaption><a href="figures/{name}.svg" aria-label="{label} SVG">SVG</a>'
        f'<a href="figures/{name}.pdf" aria-label="{label} PDF">PDF</a></figcaption></figure>'
    )


def arrival_figures(directory, name, description):
    return "".join(
        f"<article><h3>{comparison.GAPS[gap]}</h3>"
        + figure(directory, f"{name}-{gap}ms", f"{comparison.GAPS[gap]} · {description}")
        + "</article>"
        for gap in (25, 0)
    )


def load(module, manifest, override):
    if override:
        return (*module.read(override), None)
    release = ResultsRelease.read(HERE / manifest)
    with release.unpack(ROOT / "build" / "results") as directory:
        return (*module.read(directory), release)


def download_link(release, label):
    if release is None:
        return ""
    return f'<a href="{html.escape(release.asset_url)}">{html.escape(label)}</a>'


def remove_retired_assets(output):
    figures = tuple(
        f"{name}.{extension}" for name in RETIRED_FIGURES for extension in ("svg", "pdf")
    ) + RETIRED_MIXED_CHARTS
    # The previous site used these exact paths. Other files in a custom output stay in place.
    for directory, filenames in ((output / "assets", RETIRED_ASSETS), (output / "figures", figures)):
        if directory.is_symlink():
            continue
        for name in filenames:
            target = directory / name
            if target.is_file() or target.is_symlink():
                target.unlink(missing_ok=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix-results", type=Path, help="use local problem-validation measurements")
    ap.add_argument("--comparison-results", type=Path, help="use local method-comparison measurements")
    ap.add_argument("--direct-ablation-results", type=Path, help="use local Direct I/O ablation measurements")
    ap.add_argument("--output", type=Path, default=ROOT / "build" / "site")
    args = ap.parse_args()
    plan, runs, release = load(prefix, "prefix-results-release.json", args.prefix_results)
    compare_plan, compare_runs, compare_release = load(
        comparison, "comparison-results-release.json", args.comparison_results,
    )
    ablation_run, page_bytes, ablation_release = load(
        direct_ablation, "direct-ablation-results-release.json", args.direct_ablation_results,
    )
    summary = prefix.summarize(plan, runs)
    compare_summary = comparison.summarize(compare_plan, compare_runs)
    ablation_summary = direct_ablation.summarize(ablation_run, page_bytes)
    figures = args.output / "figures"
    prefix.render(summary, runs, figures)
    comparison.render(compare_summary, figures)
    direct_ablation.render(ablation_summary, figures)

    assets = args.output / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(STATIC / "style.css", assets / "style.css")
    for name, data in (
        ("prefix-summary", summary), ("comparison-summary", compare_summary),
        ("direct-ablation-summary", ablation_summary),
    ):
        (assets / f"{name}.json").write_text(
            json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8",
        )
    template = (STATIC / "index.html").read_text(encoding="utf-8")
    description = "五种方法对照，延迟越低越好，吞吐越高越好"
    if "buffered-pinned" in compare_summary["measurements"]:
        description += "。包含 Buffered I/O 普通缓冲、Direct I/O 和 Buffered I/O 锁页缓冲。"
    charts = arrival_figures(figures, "method-comparison", description)
    ablation_charts = arrival_figures(
        figures, "direct-io-ablation",
        "Direct I/O 下普通主机缓冲和 CANN 锁页路径的三种调度方法。"
        "对照 8–32 MiB 与 ≥128 MiB 的 HBM 就绪 P95 和双请求吞吐。",
    )
    prefix_chart = figure(figures, "tool-agent-prefix", "两组请求的共现率与低回载量组就绪延迟")
    overview = render_overview((ROOT / "overview.md").read_text(encoding="utf-8"))
    report = (template.replace("@@POSTER_RESULTS@@", render_poster_results(compare_summary))
              .replace("@@OVERVIEW@@", overview)
              .replace("@@PREFIX_RESULTS@@", prefix_chart)
              .replace("@@LOW_RELOAD_RANGE@@", LOW_RELOAD_RANGE)
              .replace("@@HIGH_RELOAD_RANGE@@", HIGH_RELOAD_RANGE)
              .replace("@@COMPARISON@@", charts)
              .replace("@@DIRECT_ABLATION@@", ablation_charts)
              .replace("@@DIRECT_ABLATION_DOWNLOAD@@", download_link(ablation_release, "补测原始 ZIP"))
              .replace("@@PREFIX_DOWNLOAD@@", download_link(release, "问题验证原始 ZIP"))
              .replace("@@COMPARISON_DOWNLOAD@@", download_link(compare_release, "对照原始 ZIP")))
    (args.output / "index.html").write_text(report, encoding="utf-8")
    remove_retired_assets(args.output)
    print(f"Built {args.output / 'index.html'} and Agent prefix comparison figures")


if __name__ == "__main__":
    main()
