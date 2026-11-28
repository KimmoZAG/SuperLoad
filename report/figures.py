"""Shared styling for SVG and PDF experiment figures."""

import matplotlib

matplotlib.use("Agg")
from matplotlib import pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.ticker import LogLocator, NullLocator, StrMethodFormatter


MODES = {"buffered": "Buffered", "direct": "Direct I/O"}
LOW_RELOAD_RANGE = "8–32 MiB"
HIGH_RELOAD_RANGE = "≥128 MiB"
INK = "#17242B"
MUTED = "#627078"

plt.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 10, "axes.labelsize": 10, "axes.titlesize": 11,
    "axes.titleweight": "semibold", "axes.edgecolor": "#A3B1B6", "axes.labelcolor": INK,
    "axes.spines.top": False, "axes.spines.right": False, "axes.axisbelow": True,
    "xtick.color": MUTED, "ytick.color": MUTED, "text.color": INK,
    "grid.color": "#DFE6E7", "grid.linewidth": 0.65, "svg.fonttype": "path", "svg.hashsalt": "superload",
    "pdf.fonttype": 42, "savefig.facecolor": "white",
})


def save(fig, output, name, *, pdf=True):
    fig.savefig(output / f"{name}.svg", metadata={"Date": None, "Title": name})
    if pdf:
        fig.savefig(output / f"{name}.pdf", metadata={"CreationDate": None, "Title": name})
    plt.close(fig)


def panel_title(ax, letter, title):
    ax.set_title(f"({letter})  {title}", loc="left", pad=9)


RELOAD_METRICS = (
    ("small_p95", f"{LOW_RELOAD_RANGE} P95"),
    ("large_p95", f"{HIGH_RELOAD_RANGE} P95"),
    ("gib_s", "Throughput"),
)


def render_reload_panels(groups, methods, output, name, *, mobile):
    """Draw comparison groups against shared metric axes."""
    rows, columns = len(groups), len(RELOAD_METRICS)
    if mobile:
        height = 15.5 + (rows - 2) * 7.4
        fig, axes = plt.subplots(rows * columns, 1, figsize=(5.2, height))
        fig.subplots_adjust(left=0.29, right=0.96,
                            top=1 - 0.04 * 15.5 / height, bottom=0.085 * 15.5 / height, hspace=1.05)
    else:
        height = 5.75 + (rows - 2) * 2.4
        fig, axes = plt.subplots(rows, columns, figsize=(11.4, height), sharex="col", sharey=True)
        fig.subplots_adjust(left=0.215, right=0.978, top=0.915, bottom=0.15, hspace=0.34, wspace=0.23)
    for col, (metric, label) in enumerate(RELOAD_METRICS):
        points = [v for group in groups.values() for method in methods
                  for v in group[method][metric]["trials"]]
        latency = metric != "gib_s"
        axis_label = "HBM-ready latency (ms, log)" if latency else "Pair throughput (GiB/s, log)"
        direction = "← Lower is better" if latency else "Higher is better →"
        for i, (group_label, group) in enumerate(groups.items()):
            index = columns * i + col
            ax = axes.flat[index]
            if mobile:
                ax.set_title(f"{group_label}\n{label}", loc="left", pad=8)
            else:
                if i == 0:
                    ax.set_title(label, pad=11)
                if col == 0:
                    placement = ax.get_position()
                    fig.text(0.025, (placement.y0 + placement.y1) / 2,
                             group_label.replace(" · ", "\n"), va="center", fontsize=9,
                             fontweight="semibold", linespacing=1.3)
            ax.set_xscale("log")
            ax.set_xlim(min(points) * 0.8, max(points) * 1.2)
            ax.xaxis.set_major_locator(LogLocator(base=10, subs=(1, 3) if latency else (1, 2, 5)))
            ax.xaxis.set_major_formatter(StrMethodFormatter("{x:g}"))
            ax.xaxis.set_minor_locator(NullLocator())
            if i == rows - 1 or mobile:
                ax.set_xlabel(f"{axis_label}\n{direction}")
            ax.set_yticks(range(len(methods)), [name for name, _ in methods.values()], fontsize=8.5)
            ax.set_ylim(len(methods) - 0.5, -0.5)
            ax.grid(axis="x")
            for j, (method, (_, color)) in enumerate(methods.items()):
                stats = group[method][metric]
                values = stats["trials"]
                ax.hlines(j, min(values), max(values), color=color, linewidth=1.4, alpha=0.65)
                offsets = [j + (n - (len(values) - 1) / 2) * 0.19 for n in range(len(values))]
                ax.scatter(values, offsets, facecolors="white", edgecolors=color, s=24, zorder=3)
                ax.scatter(stats["median"], j, color=color, marker="D", s=36 if method == "superload" else 28, zorder=4)
    legend = [
        Line2D([], [], marker="D", color="#4d5c64", ls="none", label="Median across trials"),
        Line2D([], [], marker="o", markerfacecolor="white", color="#4d5c64", ls="none",
               label="Individual trial"),
    ]
    fig.legend(handles=legend, loc="lower center", bbox_to_anchor=(0.53, 0.025), ncol=2, frameon=False)
    suffix = "-mobile" if mobile else ""
    save(fig, output, f"{name}{suffix}", pdf=not mobile)
