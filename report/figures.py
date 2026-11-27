"""Shared styling for SVG and PDF experiment figures."""

import matplotlib

matplotlib.use("Agg")
from matplotlib import pyplot as plt


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
