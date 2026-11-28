"""Compare measured reload latency with a swept recompute reference."""

from collections import defaultdict
from math import ceil, floor, log10
from statistics import median

from figures import INK, LOW_RELOAD_RANGE, MUTED, panel_title, save
from matplotlib import pyplot as plt
from matplotlib.ticker import LogLocator, NullLocator, PercentFormatter, StrMethodFormatter


# ThunderServe (MLSys 2025), Appendix H, Table 5, non-disaggregated baseline.
REFERENCE_MS = 884
OPTIMIZED_COLOR = "#00746D"


def paired_latencies(run):
    samples = defaultdict(list)
    for trial in run["trials"]:
        for row in trial:
            if row["gap_ms"] == 25 and row["method"] in ("superload", "fifo"):
                samples[row["pair"], row["method"]].append(row["requests"]["small"]["ready_ms"] / 1000)
    return [(median(samples[pair, "superload"]), median(samples[pair, "fifo"])) for pair in run["pairs"]]


def crossover_count(pairs, reference):
    return sum(opt < reference < fifo for opt, fifo in pairs)


def render_card(pairs):
    reference = REFERENCE_MS / 1000
    optimized, fifo = zip(*pairs)
    lower = floor(log10(min(min(optimized), min(fifo), reference) / 1.1))
    upper = ceil(log10(max(max(optimized), max(fifo), reference) * 1.1))

    def position(seconds):
        return 100 * (log10(seconds) - lower) / (upper - lower)

    rows = []
    for method, label, samples in (
        ("optimized", "优化后 SuperLoad", optimized), ("fifo", "原生 FIFO", fifo),
    ):
        start, end = min(samples), max(samples)
        rows.append(
            f'<div class="crossover-label">{label}<span>{start * 1000:,.0f}–{end * 1000:,.0f} ms</span></div>'
            '<div class="crossover-lane" aria-hidden="true">'
            f'<span class="crossover-range crossover-range-{method}" '
            f'style="--start:{position(start):.3f}%;--end:{position(end):.3f}%"></span></div>'
        )
    decades = sorted({lower, (lower + upper) // 2, upper})
    ticks = "".join(
        f'<span style="--tick:{100 * (exponent - lower) / (upper - lower):.3f}%">{10 ** exponent:g}</span>'
        for exponent in decades
    )
    return (
        '<div class="crossover-finding">'
        f'<strong>{crossover_count(pairs, reference)}/{len(pairs)} <span>对</span></strong>'
        f'<p>以 {REFERENCE_MS} ms 为重算参考，这些低回载量请求从「重算更快」变为「回载更快」。</p></div>'
        '<figure class="crossover-preview">'
        f'<div class="crossover-plot" style="--reference:{position(reference):.3f}%">'
        '<div class="crossover-guide" aria-hidden="true"><span>重算参考</span></div>'
        f'{"".join(rows)}'
        '<div class="crossover-units" aria-hidden="true">耗时（秒，对数）</div>'
        f'<div class="crossover-axis" aria-hidden="true">{ticks}</div>'
        '</div><figcaption>逐对 HBM 就绪延迟的中位数范围</figcaption></figure>'
        '<a href="#recompute-crossover">查看配对分布与论文参考 <span aria-hidden="true">↓</span></a>'
    )


def render(pairs, repeat, output):
    render_layout(pairs, repeat, output, mobile=False)
    render_layout(pairs, repeat, output, mobile=True)


def render_layout(pairs, repeat, output, *, mobile):
    reference = REFERENCE_MS / 1000
    measured = {value for pair in pairs for value in pair}
    xmin = min(min(measured), reference) * 0.75
    xmax = max(max(measured), reference) * 1.25
    edges = sorted({xmin, xmax, *measured})
    coverage = [sum(opt < (left + right) / 2 < fifo for opt, fifo in pairs) / len(pairs)
                for left, right in zip(edges, edges[1:])]
    reference_count = crossover_count(pairs, reference)

    if mobile:
        fig, (latency, crossover) = plt.subplots(2, 1, figsize=(5.1, 7.0))
        fig.subplots_adjust(left=0.19, right=0.95, bottom=0.18, top=0.945, hspace=0.72)
    else:
        fig, (latency, crossover) = plt.subplots(1, 2, figsize=(8.4, 3.95))
        fig.subplots_adjust(left=0.105, right=0.97, bottom=0.34, top=0.87, wspace=0.38)

    for axis in (latency, crossover):
        axis.set_xscale("log")
        axis.set_xlim(xmin, xmax)
        axis.xaxis.set_major_locator(LogLocator(base=10, subs=(1, 2, 5)))
        axis.xaxis.set_major_formatter(StrMethodFormatter("{x:g}"))
        axis.xaxis.set_minor_locator(NullLocator())
        axis.set_ylim(0, 1.075)
        axis.set_yticks([0, 0.25, 0.50, 0.75, 1])
        axis.yaxis.set_major_formatter(PercentFormatter(1, decimals=0))
        axis.grid(axis="y")

    optimized, fifo = zip(*pairs)
    fractions = [0, *(index / len(pairs) for index in range(1, len(pairs) + 1)), 1]
    for values, name, color, style in ((optimized, "SuperLoad", OPTIMIZED_COLOR, "-"), (fifo, "FIFO", INK, "--")):
        latency.step([xmin, *sorted(values), xmax], fractions, where="post",
                     label=name, color=color, linestyle=style, linewidth=1.65)
    panel_title(latency, "a", "Measured reload latency")
    latency.set_ylabel("Empirical CDF")
    latency.set_xlabel(f"{LOW_RELOAD_RANGE} KV-ready latency (s, log scale)")
    latency.legend(loc="upper center", bbox_to_anchor=(0.5, -0.23), ncols=2, frameon=False, fontsize=9)

    crossover.stairs(coverage, edges, color=OPTIMIZED_COLOR, linewidth=1.65, baseline=None)
    crossover.axvline(reference, color=MUTED, linestyle=":", linewidth=1.1)
    crossover.scatter(reference, reference_count / len(pairs), color=OPTIMIZED_COLOR,
                      edgecolors="white", linewidths=0.8, s=30, zorder=3)
    panel_title(crossover, "b", "Recompute crossover")
    crossover.set_ylabel(r"Pairs with $R_\mathrm{opt}<C<R_\mathrm{FIFO}$")
    crossover.set_xlabel(r"Recompute reference, $C$ (s, log scale)")
    crossover.annotate(f"ThunderServe: {reference:.3f} s\n{reference_count}/{len(pairs)} pairs",
                       xy=(reference, reference_count / len(pairs)), xytext=(0.98, 0.90),
                       textcoords="axes fraction", ha="right", va="top", fontsize=8.1,
                       arrowprops={"arrowstyle": "-", "lw": 0.75, "color": MUTED})

    protocol = f"{len(pairs)} pairs × {repeat} rounds; the high-volume request arrives 25 ms earlier."
    data_source = "Mooncake Tool & Agent trace · Ascend 910B2 · Direct I/O"
    reference_source = "Prefill reference: ThunderServe (MLSys 2025), Appendix H, Table 5."
    if mobile:
        caption = f"{data_source}\n{protocol}\nEach pair uses its median across rounds."
    else:
        caption = f"{data_source} · {len(pairs)} pairs × {repeat} rounds\n" + (
            "The high-volume request arrives 25 ms earlier. Each pair uses its median across rounds."
        )
    fig.text(0.105, 0.038, f"{caption}\n{reference_source}", color=MUTED, fontsize=8)
    save(fig, output, "recompute-crossover-mobile" if mobile else "recompute-crossover", pdf=not mobile)
