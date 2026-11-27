"""Shared styling for SVG and PDF experiment figures."""

import matplotlib

matplotlib.use("Agg")
from matplotlib import pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.ticker import LogLocator, MaxNLocator, NullFormatter, ScalarFormatter


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


def render_reload_panels(groups, methods, output, name, *, mobile, log_metrics=("small_p95", "large_p95")):
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
        fig.subplots_adjust(left=0.135, right=0.965 if rows == 3 else 0.982,
                            top=1 - 0.10 * 5.75 / height, bottom=0.19 * 5.75 / height, hspace=0.42, wspace=0.29)
    for col, (metric, label) in enumerate(RELOAD_METRICS):
        points = [v for group in groups.values() for method in methods
                  for v in group[method][metric]["trials"]]
        latency = metric != "gib_s"
        log_scale = metric in log_metrics
        if latency:
            axis_label = "KV-ready latency (ms, log)" if log_scale else "KV-ready latency (ms)"
        else:
            axis_label = "Pair throughput (GiB/s)"
        direction = "← Lower is better" if latency else "Higher is better →"
        for i, (group_label, group) in enumerate(groups.items()):
            index = rows * col + i if mobile else columns * i + col
            ax = axes.flat[index]
            panel_title(ax, chr(ord("A") + index), f"{group_label} · {label}")
            if log_scale:
                ax.set_xscale("log")
                ax.set_xlim(min(points) * 0.8, max(points) * 1.2)
                ax.xaxis.set_major_locator(LogLocator(base=10, subs=(1, 3)))
                ax.xaxis.set_major_formatter(ScalarFormatter())
                ax.xaxis.set_minor_formatter(NullFormatter())
            elif latency:
                margin = (max(points) - min(points)) * 0.12
                ax.set_xlim(min(points) - margin, max(points) + margin)
                ax.xaxis.set_major_locator(MaxNLocator(nbins=4))
            else:
                ax.set_xlim(0, max(points) * 1.10)
            ax.set_xlabel(f"{axis_label}\n{direction}" if i == rows - 1 or mobile else direction)
            ax.set_yticks(range(len(methods)), [name for name, _ in methods.values()])
            ax.set_ylim(len(methods) - 0.5, -0.5)
            ax.grid(axis="x")
            for j, (method, (_, color)) in enumerate(methods.items()):
                stats = group[method][metric]
                values = stats["trials"]
                ax.hlines(j, min(values), max(values), color=color, linewidth=1.4, alpha=0.65)
                offsets = [j + (n - (len(values) - 1) / 2) * 0.19 for n in range(len(values))]
                ax.scatter(values, offsets, facecolors="white", edgecolors=color, s=24, zorder=3)
                ax.scatter(stats["median"], j, color=color, marker="D", s=28, zorder=4)
    legend = [
        Line2D([], [], marker="D", color="#4d5c64", ls="none", label="Median across trials"),
        Line2D([], [], marker="o", markerfacecolor="white", color="#4d5c64", ls="none",
               label="Individual trial"),
    ]
    fig.legend(handles=legend, loc="lower center", bbox_to_anchor=(0.53, 0.025), ncol=2, frameon=False)
    suffix = "-mobile" if mobile else ""
    save(fig, output, f"{name}{suffix}", pdf=not mobile)
