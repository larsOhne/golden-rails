"""Figures and tables of the year evaluation (evaluate_year.py).

    python -m golden_rails.report_year

Writes results/year_ranking.png, results/year_violins.png, results/year_portfolio.png and results/year_rank_stability.csv.
"""
from __future__ import annotations

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.patches import Patch

from . import measures as M
from .plots import INK, INK2, RES, SURF, _style
from .sim import DAY_FACTOR

COLORS = {"E": "#1baf7a", "E2": "#0f7a55", "T1": "#2a78d6", "T2": "#184f95", "PLAT": "#8a5cd1", "THR": "#8a5cd1",
          "NE": "#8a5cd1", "RES": "#d4a017", "FIT": "#d4a017", "P": "#eb6834"}
LABELS = {"E": "ETCS (mit Signalen)", "E2": "Nur-ETCS inkl. Nachrüstung", "T1": "Neue Gleise", "PLAT": "Knoten",
          "RES": "Flotte (Reserve, Nachrüstung)", "P": "Geplantes Projekt"}


def ranking(cand, n=30):
    top = cand[np.isfinite(cand.kpi_paxmin_per_keur)].head(n).iloc[::-1]
    fig, ax = plt.subplots(figsize=(9.5, 0.34 * n + 1.6), dpi=150, facecolor=SURF)
    _style(ax)
    y = np.arange(len(top))
    k = top.kpi_paxmin_per_keur
    ax.barh(y, k, color=[COLORS.get(o, INK2) for o in top.option], height=0.7, edgecolor=SURF, linewidth=1.5)
    ax.errorbar(k, y, xerr=[k - top.kpi_lo.clip(lower=0), top.kpi_hi - k], fmt="none", ecolor=INK2, elinewidth=1)
    ax.set_yticks(y)
    ax.set_yticklabels([f"{nm}  ({c:,.0f} Mio €)".replace(",", ".") for nm, c in zip(top.name, top.capex_meur)],
                       fontsize=7.5, color=INK)
    ax.set_xlabel("Eingesparte Fahrgast-Verspätungsminuten pro Jahr je 1.000 € Jahreskosten (inkl. Ausfälle)",
                  fontsize=8, color=INK2)
    ax.set_title("Wertvollste Maßnahmen, Jahresbewertung (12 Monatsfahrpläne, 120 Tage, 95%-KI)",
                 loc="left", fontsize=10.5, color=INK)
    ax.legend(handles=[Patch(color=COLORS[o], label=l) for o, l in LABELS.items()], fontsize=7, frameon=False,
              loc="lower right")
    fig.tight_layout()
    fig.savefig(RES / "year_ranking.png")
    plt.close(fig)


def portfolio(path):
    fig, ax = plt.subplots(figsize=(9, 4.6), dpi=150, facecolor=SURF)
    _style(ax)
    ax.grid(axis="y", color="#e6e5e0", lw=0.8)
    x = path.capex_meur / 1000
    ax.plot(x, 100 * path.pax_punct, "-o", color="#2a78d6", label="alle Fahrgäste (Halte < 6 min)")
    ax.plot(x, 100 * path.punct_FV, "-o", color="#eb6834", label="ICE/IC (Halte < 6 min)")
    for r, xi in zip(path.itertuples(), x):
        if r.step > 0:
            ax.annotate(f"{r.step}", (xi, 100 * r.pax_punct), textcoords="offset points", xytext=(0, 6),
                        fontsize=7, color=INK2, ha="center")
    ax.set_xlabel("Kumulierte Investition (Mrd. €)", fontsize=8, color=INK2)
    ax.set_ylabel("Pünktlichkeit (%)", fontsize=8, color=INK2)
    ax.set_title("Greedy-Portfolio: Pünktlichkeit über kumulierter Investition", loc="left", fontsize=10.5, color=INK)
    ax.legend(fontsize=8, frameon=False)
    fig.tight_layout()
    fig.savefig(RES / "year_portfolio.png")
    plt.close(fig)


def rank_stability(cand, per_day, n_boot=2000, top=10, seed=0):
    """Bootstrap over simulated days: probability that a measure is in the top ``top`` by KPI."""
    cand = cand[np.isfinite(cand.kpi_paxmin_per_keur)]
    mat = per_day.pivot(index="day", columns="id", values="pax_min_saved")[list(cand.id)]
    cost = cand.set_index("id").annual_cost_meur.reindex(mat.columns).values
    rng = np.random.default_rng(seed)
    hits = np.zeros(mat.shape[1])
    ranks = []
    vals = mat.values
    for _ in range(n_boot):
        idx = rng.integers(0, len(vals), len(vals))
        kpi = vals[idx].mean(axis=0) / cost
        order = np.argsort(-kpi)
        r = np.empty_like(order)
        r[order] = np.arange(1, len(order) + 1)
        ranks.append(r)
        hits[order[:top]] += 1
    ranks = np.array(ranks)
    out = pd.DataFrame({"id": mat.columns, "p_top10": hits / n_boot,
                        "rank_median": np.median(ranks, axis=0), "rank_p05": np.percentile(ranks, 5, axis=0),
                        "rank_p95": np.percentile(ranks, 95, axis=0)})
    return cand[["id", "name", "option"]].merge(out, on="id").sort_values("rank_median")


def cost_sensitivity(cand):
    """Own unit costs at the low / high end of their range; planned projects keep their published capex."""
    rows = []
    for which in ("low", "high"):
        d = cand.copy()
        f = np.where(d.option == "P", 1.0, M.RANGE[which])
        d["kpi"] = d.kpi_paxmin_per_keur / f
        d = d.sort_values("kpi", ascending=False).reset_index(drop=True)
        best_p = d.index[d.option == "P"].min() + 1
        rows.append({"costs": which, "best_planned_rank": int(best_p), "top5": " | ".join(d.name.head(5))})
    return pd.DataFrame(rows)


VCOL = {"ETCS": "#2a78d6", "Gleise": "#eb6834", "Knoten": "#1baf7a", "Flotte": "#eda100",
        "Fahrplan / Projekt": "#e87ba4"}
VGRP = {"E": "ETCS", "E2": "ETCS", "T1": "Gleise", "T2": "Gleise", "PLAT": "Knoten", "THR": "Knoten", "NE": "Knoten",
        "RES": "Flotte", "FIT": "Flotte", "TT": "Fahrplan / Projekt", "P": "Fahrplan / Projekt"}


def violins(cand, per_day, n=16):
    """Distribution over the simulated days of (a) passenger-minutes saved per day and (b) the KPI of that day."""
    best = list(cand[np.isfinite(cand.kpi_paxmin_per_keur)].head(n - 2).id)
    best += [i for i in ("TT-soft", "P-C10") if i not in best]
    sel = cand.set_index("id").loc[best]
    pd_ = per_day.pivot(index="day", columns="id", values="pax_min_saved")
    fig, axes = plt.subplots(1, 2, figsize=(13, 0.42 * len(best) + 1.8), dpi=150, facecolor=SURF, sharey=True)
    y = np.arange(len(best))[::-1]
    for ax, mode in zip(axes, ("abs", "kpi")):
        _style(ax)
        data = []
        for i in best:
            v = pd_[i].dropna().values
            if mode == "abs":
                data.append(v / 1000)
            else:
                c = sel.loc[i, "annual_cost_meur"]
                data.append(v * DAY_FACTOR / (c * 1e6) * 1000 / 1000 if c > 0 else np.full_like(v, np.nan))
        for yi, v, i in zip(y, data, best):
            col = VCOL[VGRP[sel.loc[i, "option"]]]
            v = v[np.isfinite(v)]
            if len(v) == 0:
                ax.text(0, yi, "kostenlos", va="center", fontsize=7, color=INK2)
                continue
            part = ax.violinplot([v], positions=[yi], orientation="horizontal", widths=0.8, showextrema=False)
            for b in part["bodies"]:
                b.set_facecolor(col)
                b.set_edgecolor(SURF)
                b.set_linewidth(1.5)
                b.set_alpha(0.85)
            q1, med, q3 = np.percentile(v, [25, 50, 75])
            ax.plot([q1, q3], [yi, yi], color=INK, lw=2, solid_capstyle="round")
            ax.plot([v.mean()], [yi], "o", ms=4, color=SURF, mec=INK, mew=1.2)
        ax.axvline(0, color=INK2, lw=0.8)
        ax.grid(axis="y", visible=False)
        ax.set_xlim(*((-1200, 2800) if mode == "abs" else (-60, 120)))
    axes[0].set_yticks(y)
    axes[0].set_yticklabels([f"{sel.loc[i, 'name']}  ({sel.loc[i, 'capex_meur']:,.0f} Mio €)".replace(",", ".")
                             for i in best], fontsize=7.5, color=INK)
    axes[0].set_xlabel("Fahrgast-Verspätungsminuten gespart pro Tag (Tsd., Ausschnitt)", fontsize=8, color=INK2)
    axes[1].set_xlabel("KPI des Tages: Fahrgast-min/Jahr je 1.000 € (Tsd., Ausschnitt)",
                       fontsize=8, color=INK2)
    axes[0].set_title("Nutzen pro Tag", loc="left", fontsize=10, color=INK)
    axes[1].set_title("Nutzen je Euro", loc="left", fontsize=10, color=INK)
    fig.suptitle("Streuung über 120 simulierte Tage (12 Monatsfahrpläne): Balken = mittlere 50 %, Punkt = Mittelwert",
                 x=0.01, ha="left", fontsize=10.5, color=INK)
    fig.legend(handles=[Patch(color=c, label=l) for l, c in VCOL.items()], fontsize=7.5, frameon=False,
               loc="lower right", ncol=5)
    fig.tight_layout(rect=(0, 0.03, 1, 0.97))
    fig.savefig(RES / "year_violins.png")
    plt.close(fig)


def main():
    cand = pd.read_csv(RES / "year_candidates.csv")
    path = pd.read_csv(RES / "year_portfolio.csv")
    per_day = pd.read_csv(RES / "year_per_day.csv")
    ranking(cand)
    violins(cand, per_day)
    portfolio(path)
    st = rank_stability(cand, per_day)
    st.to_csv(RES / "year_rank_stability.csv", index=False)
    pd.set_option("display.width", 250)
    print(st.head(20).to_string(index=False))
    print(cost_sensitivity(cand).to_string(index=False))
    print(f"portfolio: {path.capex_meur.iloc[-1]:,.0f} M EUR capex, "
          f"{(path.pax_delay_min_per_day.iloc[0] - path.pax_delay_min_per_day.iloc[-1]) * DAY_FACTOR / 1e6:,.0f} "
          f"m pax-min/yr saved")


if __name__ == "__main__":
    main()
