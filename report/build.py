"""Build the Agent prefix report from the pinned measurement Releases."""

import argparse
import html
import json
import shutil
from pathlib import Path

import comparison
import prefix
from release_data import ResultsRelease


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
STATIC = HERE / "static"


def figure(name, alt):
    label = html.escape(alt)
    return (
        '<figure class="paper-figure"><picture>'
        f'<source media="(max-width: 48rem)" srcset="figures/{name}-mobile.svg">'
        f'<img src="figures/{name}.svg" alt="{label}" decoding="async"></picture>'
        f'<figcaption><a href="figures/{name}.svg" aria-label="{label} SVG">SVG</a>'
        f'<a href="figures/{name}.pdf" aria-label="{label} PDF">PDF</a></figcaption></figure>'
    )


def load(module, manifest, override):
    if override:
        return (*module.read(override), "")
    release = ResultsRelease.read(HERE / manifest)
    with release.unpack(ROOT / "build" / "results") as directory:
        return (*module.read(directory), release.page_url)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix-results", type=Path, help="use local problem-validation measurements")
    ap.add_argument("--comparison-results", type=Path, help="use local method-comparison measurements")
    ap.add_argument("--output", type=Path, default=ROOT / "build" / "site")
    args = ap.parse_args()
    plan, runs, release = load(prefix, "prefix-results-release.json", args.prefix_results)
    compare_plan, compare_runs, compare_release = load(
        comparison, "comparison-results-release.json", args.comparison_results,
    )
    summary = prefix.summarize(plan, runs)
    compare_summary = comparison.summarize(compare_plan, compare_runs)
    figures = args.output / "figures"
    prefix.render(summary, runs, figures)
    comparison.render(compare_summary, figures)

    assets = args.output / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(STATIC / "style.css", assets / "style.css")
    for name, data in (("prefix-summary", summary), ("comparison-summary", compare_summary)):
        (assets / f"{name}.json").write_text(
            json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8",
        )
    template = (STATIC / "index.html").read_text(encoding="utf-8")
    charts = "".join(
        f'<article><h3>{comparison.GAPS[gap]}</h3>'
        + figure(f"method-comparison-{gap}ms", f"{comparison.GAPS[gap]}时五种方法的延迟与吞吐")
        + '</article>'
        for gap in (25, 0)
    )
    report = (template.replace("@@PREFIX_RESULTS@@", figure("tool-agent-prefix", "共现率与小回载就绪时延"))
              .replace("@@COMPARISON@@", charts)
              .replace("@@PREFIX_RELEASE@@", html.escape(release or "https://github.com/KimmoZAG/SuperLoad/releases"))
              .replace("@@COMPARISON_RELEASE@@",
                       html.escape(compare_release or "https://github.com/KimmoZAG/SuperLoad/releases")))
    (args.output / "index.html").write_text(report, encoding="utf-8")
    print(f"Built {args.output / 'index.html'} and Agent prefix comparison figures")


if __name__ == "__main__":
    main()
