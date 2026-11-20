"""Static figures: KPI ranking bar chart and network map."""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.colors import LinearSegmentedColormap

from .network import load_network

ROOT = Path(__file__).resolve().parent.parent
RES = ROOT / "results"
INK, INK2, SURF, GRID = "#0b0b0b", "#52514e", "#fcfcfb", "#e6e5e0"
BLUE, ORANGE = "#2a78d6", "#eb6834"
SEQ = LinearSegmentedColormap.from_list("seq", ["#cde2fb", "#86b6ef", "#2a78d6", "#184f95", "#0d366b"])


def _style(ax):
    ax.set_facecolor(SURF)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=INK2, length=0)
    ax.grid(axis="x", color=GRID, lw=0.8)
    ax.set_axisbelow(True)


def ranking(df: pd.DataFrame, n=20, path=RES / "kpi_ranking.png"):
    top = df.head(n).iloc[::-1]
    fig, ax = plt.subplots(figsize=(9, 0.38 * n + 1.4), dpi=150, facecolor=SURF)
    _style(ax)
    colors = [ORANGE if k == "planned project" else BLUE for k in top.kind]
    y = range(len(top))
    ax.barh(y, top.kpi_min_per_keur, color=colors, height=0.7, edgecolor=SURF, linewidth=2)
    ax.errorbar(top.kpi_min_per_keur, y,
                xerr=[top.kpi_min_per_keur - top.kpi_lo, top.kpi_hi - top.kpi_min_per_keur],
                fmt="none", ecolor=INK2, elinewidth=1, capsize=0)
    ax.set_yticks(list(y))
    ax.set_yticklabels([f"{nm}  ({km:.0f} km)" for nm, km in zip(top.name, top.route_km)],
                       color=INK, fontsize=8)
    ax.set_xlabel("Delay minutes saved per year per €1,000 of annual cost (capex annuity + opex)",
                  color=INK2, fontsize=8)
    ax.set_title("Delay reduction per euro: top candidates", loc="left", color=INK, fontsize=11)
    from matplotlib.patches import Patch
    ax.legend(handles=[Patch(color=ORANGE, label="Planned project (BVWP / Deutschlandtakt)"),
                       Patch(color=BLUE, label="Generic +1 track on existing line")],
              frameon=False, fontsize=8, loc="lower right", labelcolor=INK)
    fig.tight_layout()
    fig.savefig(path, facecolor=SURF)
    plt.close(fig)


def network_map(df: pd.DataFrame, hot: pd.DataFrame, path=RES / "network_map.png"):
    net = load_network()
    nodes = net.nodes
    g = df[df.kind == "generic +1 track"].copy()
    g["seg"] = g.id.str[2:]
    kpi = dict(zip(g.seg, g.kpi_min_per_keur.clip(lower=0)))
    vmax = max(kpi.values()) if kpi else 1
    fig, ax = plt.subplots(figsize=(7.5, 9), dpi=150, facecolor=SURF)
    ax.set_facecolor(SURF)
    ax.set_axis_off()
    for s in net.segments.values():
        a, b = nodes.loc[s.a], nodes.loc[s.b]
        v = kpi.get(s.id)
        color = SEQ(0.15 + 0.85 * v / vmax) if v is not None else "#c3c2b7"
        lw = 1.0 + 0.9 * s.tracks
        ax.plot([a.lon, b.lon], [a.lat, b.lat], color=color, lw=lw, solid_capstyle="round", zorder=1)
    for nid, r in nodes.iterrows():
        if r.major:
            ax.scatter(r.lon, r.lat, s=14, color=INK, zorder=3, edgecolor=SURF, linewidth=1)
            ax.annotate(r["name"].replace(" Hbf", ""), (r.lon, r.lat), xytext=(3, 3),
                        textcoords="offset points", fontsize=6, color=INK2)
    best = g.sort_values("kpi_min_per_keur", ascending=False).head(5)
    for rank, row in enumerate(best.itertuples(), 1):
        s = net.segments[row.seg]
        a, b = nodes.loc[s.a], nodes.loc[s.b]
        ax.annotate(f"#{rank}", ((a.lon + b.lon) / 2, (a.lat + b.lat) / 2), fontsize=8,
                    color=INK, weight="bold", ha="center", va="center",
                    bbox=dict(boxstyle="round,pad=0.2", fc=SURF, ec=INK2, lw=0.6))
    sm = plt.cm.ScalarMappable(cmap=SEQ, norm=plt.Normalize(0, vmax))
    cb = fig.colorbar(sm, ax=ax, shrink=0.4, pad=0.01)
    cb.set_label("+1 track: delay-min saved / €1,000", color=INK2, fontsize=8)
    cb.outline.set_visible(False)
    cb.ax.tick_params(colors=INK2, labelsize=7)
    ax.set_title("Value of one extra track per segment\n(line width = current tracks; grey = already 4-track, not tested)",
                 loc="left", color=INK, fontsize=10)
    ax.set_aspect(1.6)
    fig.tight_layout()
    fig.savefig(path, facecolor=SURF)
    plt.close(fig)


def main():
    df = pd.read_csv(RES / "candidates.csv")
    hot = pd.read_csv(RES / "baseline_hotspots.csv")
    ranking(df)
    network_map(df, hot)


if __name__ == "__main__":
    main()
