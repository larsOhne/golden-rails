"""Static figures: KPI ranking bar chart and network map."""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import LinearSegmentedColormap

from .network import load_network

ROOT = Path(__file__).resolve().parent.parent
RES = ROOT / "results"
INK, INK2, SURF, GRID = "#0b0b0b", "#52514e", "#fcfcfb", "#e6e5e0"
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
SEQ = LinearSegmentedColormap.from_list("seq", ["#cde2fb", "#86b6ef", "#2a78d6", "#184f95", "#0d366b"])


def _style(ax):
    ax.set_facecolor(SURF)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=INK2, length=0)
    ax.grid(axis="x", color=GRID, lw=0.8)
    ax.set_axisbelow(True)


def _color(row):
    if row.kind == "planned project":
        return ORANGE
    return AQUA if str(row.option) == "E" else BLUE


def ranking(df: pd.DataFrame, n=20, path=RES / "kpi_ranking.png", title=None):
    top = df.sort_values("kpi_paxmin_per_keur", ascending=False).head(n).iloc[::-1]
    fig, ax = plt.subplots(figsize=(9, 0.38 * n + 1.4), dpi=150, facecolor=SURF)
    _style(ax)
    colors = [_color(r) for r in top.itertuples()]
    y = range(len(top))
    k = top.kpi_paxmin_per_keur
    ax.barh(y, k, color=colors, height=0.7, edgecolor=SURF, linewidth=2)
    ax.errorbar(k, y, xerr=[k - top.kpi_lo, top.kpi_hi - k],
                fmt="none", ecolor=INK2, elinewidth=1, capsize=0)
    ax.set_yticks(list(y))
    ax.set_yticklabels([f"{nm}  ({km:.0f} km)" for nm, km in zip(top.name, top.route_km)],
                       color=INK, fontsize=8)
    ax.set_xlabel("Eingesparte Fahrgast-Verspätungsminuten pro Jahr je 1.000 € Jahreskosten (Annuität + Betrieb)",
                  color=INK2, fontsize=8)
    ax.set_title(title or "Fahrgastverspätung je Euro: beste Maßnahmen", loc="left", color=INK, fontsize=11)
    from matplotlib.patches import Patch
    present = set(colors)
    handles = [h for h in (Patch(color=BLUE, label="Neue Gleise im Korridor (Kostenmodell)"),
                           Patch(color=AQUA, label="ETCS-Ausrüstung (konstante Kosten/km)"),
                           Patch(color=ORANGE, label="Geplantes Projekt (BVWP / Deutschlandtakt)"))
               if h.get_facecolor() and matplotlib.colors.to_hex(h.get_facecolor()) in present]
    ax.legend(handles=handles,
              frameon=False, fontsize=8, loc="lower right", labelcolor=INK)
    fig.tight_layout()
    fig.savefig(path, facecolor=SURF)
    plt.close(fig)


def network_map(df: pd.DataFrame, hot: pd.DataFrame, path=RES / "network_map.png"):
    net = load_network()
    nodes = net.nodes
    g = df[df.kind == "corridor option"].copy()
    g["seg"] = g.segment
    g = g.sort_values("kpi_paxmin_per_keur", ascending=False).drop_duplicates("seg")
    kpi = dict(zip(g.seg, g.kpi_paxmin_per_keur.clip(lower=0)))
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
    best = g.head(5)
    for rank, row in enumerate(best.itertuples(), 1):
        s = net.segments[row.seg]
        a, b = nodes.loc[s.a], nodes.loc[s.b]
        ax.annotate(f"#{rank}", ((a.lon + b.lon) / 2, (a.lat + b.lat) / 2), fontsize=8,
                    color=INK, weight="bold", ha="center", va="center",
                    bbox=dict(boxstyle="round,pad=0.2", fc=SURF, ec=INK2, lw=0.6))
    sm = plt.cm.ScalarMappable(cmap=SEQ, norm=plt.Normalize(0, vmax))
    cb = fig.colorbar(sm, ax=ax, shrink=0.4, pad=0.01)
    cb.set_label("beste Maßnahme: Fahrgast-Verspätungsmin. / 1.000 €", color=INK2, fontsize=8)
    cb.outline.set_visible(False)
    cb.ax.tick_params(colors=INK2, labelsize=7)
    ax.set_title("Beste Maßnahme je Korridor (+1/+2 Gleise oder ETCS)\nLinienbreite = heutige Gleiszahl; #n = Rang",
                 loc="left", color=INK, fontsize=10)
    ax.set_aspect(1.6)
    fig.tight_layout()
    fig.savefig(path, facecolor=SURF)
    plt.close(fig)


def main():
    df = pd.read_csv(RES / "candidates.csv")
    tracks = pd.read_csv(RES / "candidates_tracks.csv")
    screen = pd.read_csv(RES / "screening.csv")
    hot = pd.read_csv(RES / "baseline_hotspots.csv")
    build = pd.concat([tracks[tracks.option.isin(["T1", "T2", "T1E"])], df[df.kind == "planned project"]])
    ranking(build, 20, RES / "kpi_ranking_new_track.png",
            "Neubau von Gleisen: eingesparte Fahrgastverspätung je Euro")
    ranking(pd.concat([df, tracks]).drop_duplicates("id"), 20, RES / "kpi_ranking_all.png",
            "Alle Maßnahmen inkl. ETCS: eingesparte Fahrgastverspätung je Euro")
    network_map(screen, hot)
    convergence()



def convergence(path=RES / "convergence.png", ids=None):
    """Running KPI estimate and 95 % band vs. number of Monte-Carlo days."""
    from .sim import DAY_FACTOR
    per = pd.read_csv(RES / "per_day_tracks.csv")
    cand = pd.read_csv(RES / "candidates_tracks.csv").set_index("id")
    if ids is None:
        ids = list(cand[cand.option.isin(["T1"])].sort_values("kpi_paxmin_per_keur", ascending=False).index[:4])
    colors = ["#2a78d6", "#eb6834", "#1baf7a", "#e87ba4"]
    fig, ax = plt.subplots(figsize=(9, 4.8), dpi=150, facecolor=SURF)
    _style(ax)
    ax.grid(axis="y", color=GRID, lw=0.8)
    for c, i in zip(colors, ids):
        x = per[per.id == i].sort_values("day").pax_min_saved.to_numpy()
        n = np.arange(1, len(x) + 1)
        k = DAY_FACTOR / (cand.loc[i, "annual_cost_meur"] * 1e6) * 1000
        mean = np.cumsum(x) / n
        sd = np.array([x[:j].std(ddof=1) if j > 1 else np.nan for j in n])
        half = 1.96 * sd / np.sqrt(n)
        m = n >= 5
        ax.fill_between(n[m], (mean - half)[m] * k, (mean + half)[m] * k, color=c, alpha=0.15, lw=0)
        ax.plot(n[m], mean[m] * k, color=c, lw=2, label=cand.loc[i, "name"])
    ax.set_xlabel("Anzahl simulierter Tage (Monte-Carlo-Stichproben)", color=INK2, fontsize=8)
    ax.set_ylabel("Fahrgast-Verspätungsmin. / 1.000 €", color=INK2, fontsize=8)
    ax.set_title("Konvergenz: Schätzwert und 95-%-Band der besten Gleis-Maßnahmen", loc="left",
                 color=INK, fontsize=11)
    ax.legend(frameon=False, fontsize=8, labelcolor=INK)
    fig.tight_layout()
    fig.savefig(path, facecolor=SURF)
    plt.close(fig)


if __name__ == "__main__":
    main()
