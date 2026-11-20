"""Render a clip of one simulated operating day (MP4).

Trains move along their blocks, coloured by delay versus the timetable;
incidents (track faults, full closures) appear on the map while active.

    python -m golden_rails.clip --seed 3147 --out results/clip_day.mp4
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.animation import FFMpegWriter
from matplotlib.lines import Line2D

from .network import load_network, place_name, segment_name
from .sim import DisruptionModel, build_trains, plan_timetable, simulate_day

ROOT = Path(__file__).resolve().parent.parent

BG, PANEL, NET, INK, INK2, MUTED = "#0d0d0d", "#1a1a19", "#383835", "#f0efec", "#c3c2b7", "#8a897f"
GOOD, WARN, CRIT = "#0ca30c", "#fab219", "#d03b3b"
TYPE_STYLE = {"FV": ("o", 34, "ICE/IC"), "NV": ("o", 12, "RE/RB"),
              "S": ("o", 8, "S-Bahn/Tram"), "GV": ("s", 14, "Güterzug")}


def delay_color(d):
    return np.where(d < 6, 0, np.where(d < 15, 1, 2))


def render(seed=3147, out=ROOT / "results" / "clip_day.mp4", t0=5 * 60, t1=24 * 60,
           step=1.5, fps=30, changes: str = "", title_suffix: str = "", snapshot_t: float = 13.5 * 60):
    net = load_network()
    if changes:
        net.apply_changes(changes)
    trains = build_trains(net)
    plan_timetable(net, trains)
    trace: list = []
    res = simulate_day(net, trains, DisruptionModel(), seed, trace=trace)
    incidents = res["incidents"]

    nodes = net.nodes
    lon = nodes.lon.to_dict()
    lat = nodes.lat.to_dict()
    tr = np.array([(t[1], t[2], t[5], t[4], t[6]) for t in trace], dtype=float)
    te, tx, dirs, blk, dly = tr.T
    dly = np.nan_to_num(dly, nan=0.0)
    idx = np.array([t[0] for t in trace])
    ttype = np.array([trains[i].ttype for i in idx])
    seg = [net.segments[t[3]] for t in trace]
    nb = np.array([s.n_blocks() for s in seg], dtype=float)
    ax_ = np.array([lon[s.a] for s in seg]); ay_ = np.array([lat[s.a] for s in seg])
    bx_ = np.array([lon[s.b] for s in seg]); by_ = np.array([lat[s.b] for s in seg])
    # small lateral offset so the two directions are distinguishable
    vx, vy = bx_ - ax_, by_ - ay_
    norm = np.hypot(vx, vy) + 1e-9
    ox, oy = -vy / norm * 0.035 * dirs, vx / norm * 0.035 * dirs
    order = np.argsort(te)
    te, tx, dirs, blk, dly, ttype, nb = te[order], tx[order], dirs[order], blk[order], dly[order], ttype[order], nb[order]
    ax_, ay_, bx_, by_, ox, oy = ax_[order], ay_[order], bx_[order], by_[order], ox[order], oy[order]
    max_occ = float(np.max(tx - te))

    fig = plt.figure(figsize=(12.8, 7.2), dpi=100, facecolor=BG)
    ax = fig.add_axes([0.0, 0.0, 0.66, 1.0], facecolor=BG)
    ax.set_axis_off()
    ax.set_xlim(5.6, 15.4)
    ax.set_ylim(47.3, 54.2)
    ax.set_aspect(1.55)
    for s in net.segments.values():
        ax.plot([lon[s.a], lon[s.b]], [lat[s.a], lat[s.b]], color=NET, lw=0.8 + 1.1 * s.tracks,
                solid_capstyle="round", zorder=1)
    for nid, r in nodes.iterrows():
        if r.major:
            ax.annotate(place_name(nodes, nid), (r.lon, r.lat), xytext=(4, 4), textcoords="offset points",
                        fontsize=7, color=MUTED, zorder=2)

    pal = np.array([matplotlib.colors.to_rgba(c) for c in (GOOD, WARN, CRIT)])
    scat = {}
    for k, (m, sz, _) in TYPE_STYLE.items():
        scat[k] = ax.scatter([], [], s=sz, marker=m, linewidths=0.4, edgecolors=BG, zorder=5 if k == "FV" else 4)
    inc_full = ax.scatter([], [], s=260, marker="X", color=CRIT, edgecolors=INK, linewidths=1.0, zorder=7)
    inc_track = ax.scatter([], [], s=150, marker="o", facecolors="none", edgecolors=WARN, linewidths=2, zorder=6)

    # right panel
    pn = fig.add_axes([0.66, 0.0, 0.34, 1.0], facecolor=PANEL)
    pn.set_axis_off()
    pn.set_xlim(0, 1)
    pn.set_ylim(0, 1)
    pn.text(0.06, 0.94, "golden rails", color="#f3c447", fontsize=15, weight="bold")
    pn.text(0.06, 0.905, f"Simulierter Betriebstag (Monte-Carlo-Tag {seed}){title_suffix}", color=INK2, fontsize=8.5)
    clock = pn.text(0.06, 0.80, "", color=INK, fontsize=34, weight="bold", family="monospace")
    stats = pn.text(0.06, 0.585, "", color=INK, fontsize=10, family="monospace", va="bottom", linespacing=1.5)
    inc_txt = pn.text(0.06, 0.555, "", color=INK2, fontsize=8.5, va="top", linespacing=1.45)
    handles = [Line2D([], [], marker="o", ls="", color=c, label=l, markersize=7)
               for c, l in ((GOOD, "pünktlich (< 6 min)"), (WARN, "6–15 min verspätet"), (CRIT, "> 15 min verspätet"))]
    handles += [Line2D([], [], marker="o", ls="", color=INK2, markersize=9, label="ICE/IC (groß), RE, S-Bahn"),
                Line2D([], [], marker="s", ls="", color=INK2, markersize=6, label="Güterzug"),
                Line2D([], [], marker="X", ls="", color=CRIT, markeredgecolor=INK, markersize=10, label="Vollsperrung"),
                Line2D([], [], marker="o", ls="", markerfacecolor="none", markeredgecolor=WARN, markeredgewidth=2,
                       markersize=9, label="Gleisstörung (1 Gleis)")]
    pn.legend(handles=handles, loc="lower left", bbox_to_anchor=(0.03, 0.02), frameon=False, fontsize=8,
              labelcolor=INK2)
    # running chart: share of ICE/IC in service that are > 6 min late
    ch = fig.add_axes([0.70, 0.245, 0.27, 0.10], facecolor=PANEL)
    for sp in ch.spines.values():
        sp.set_visible(False)
    ch.tick_params(colors=MUTED, labelsize=7, length=0)
    ch.set_xlim(t0 / 60, t1 / 60)
    ch.set_ylim(0, 80)
    ch.set_yticks([0, 40, 80])
    ch.set_yticklabels(["0 %", "40 %", "80 %"])
    ch.set_xticks([6, 12, 18, 24])
    ch.set_xticklabels(["6 Uhr", "12", "18", "24"])
    ch.grid(axis="y", color=NET, lw=0.6)
    ch.set_title("ICE/IC unterwegs mit > 6 min Verspätung", color=INK2, fontsize=8, loc="left")
    line, = ch.plot([], [], color=WARN, lw=1.8)
    hist_t, hist_v = [], []

    times = np.arange(t0, t1, step)
    lo_ptr = 0

    def frame(t):
        nonlocal lo_ptr
        hi = np.searchsorted(te, t, side="right")
        lo = np.searchsorted(te, t - max_occ, side="left")
        sl = slice(lo, hi)
        act = (tx[sl] > t)
        f = np.clip((t - te[sl]) / np.maximum(tx[sl] - te[sl], 1e-6), 0, 1)
        pos = np.where(dirs[sl] > 0, (blk[sl] + f) / nb[sl], (blk[sl] + 1 - f) / nb[sl])
        x = ax_[sl] + (bx_[sl] - ax_[sl]) * pos + ox[sl]
        y = ay_[sl] + (by_[sl] - ay_[sl]) * pos + oy[sl]
        col = delay_color(dly[sl])
        tt = ttype[sl]
        counts = {}
        for k in TYPE_STYLE:
            m = act & (tt == k)
            scat[k].set_offsets(np.c_[x[m], y[m]] if m.any() else np.empty((0, 2)))
            scat[k].set_facecolors(pal[col[m]] if m.any() else np.empty((0, 4)))
            counts[k] = (int(m.sum()), int((m & (col == 0)).sum()))
        fvn, fvp = counts["FV"]
        share_late = 100 * (fvn - fvp) / fvn if fvn else 0.0
        hist_t.append(t / 60)
        hist_v.append(share_late)
        line.set_data(hist_t, hist_v)

        full_xy, track_xy, lines = [], [], []
        for kind, sid, bi, k, a, b in incidents:
            if a <= t < b:
                s = net.segments[sid]
                p = (bi + 0.5) / s.n_blocks()
                xy = (lon[s.a] + (lon[s.b] - lon[s.a]) * p, lat[s.a] + (lat[s.b] - lat[s.a]) * p)
                (full_xy if kind == "full" else track_xy).append(xy)
                what = "Vollsperrung" if kind == "full" else "Gleisstörung"
                lines.append(f"{what} {segment_name(net, sid)}  (noch {int(b - t)} min)")
        inc_full.set_offsets(np.array(full_xy) if full_xy else np.empty((0, 2)))
        inc_track.set_offsets(np.array(track_xy) if track_xy else np.empty((0, 2)))
        shown = lines[:4] + ([f"… und {len(lines) - 4} weitere"] if len(lines) > 4 else [])
        inc_txt.set_text("Aktive Störungen:\n" + ("\n".join(shown) if lines else "keine"))
        h, mnt = divmod(int(t), 60)
        clock.set_text(f"{h % 24:02d}:{mnt:02d}")
        stats.set_text("Züge unterwegs (davon pünktlich)\n" + "\n".join(
            f"{TYPE_STYLE[k][2]:<12} {counts[k][0]:>4}  ({100 * counts[k][1] / counts[k][0]:.0f} %)"
            if counts[k][0] else f"{TYPE_STYLE[k][2]:<12} {0:>4}" for k in TYPE_STYLE))

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    writer = FFMpegWriter(fps=fps, codec="libx264", bitrate=2500,
                          extra_args=["-pix_fmt", "yuv420p", "-movflags", "+faststart"])
    with writer.saving(fig, str(out), dpi=100):
        for i, t in enumerate(times):
            frame(t)
            writer.grab_frame(facecolor=BG)
            if abs(t - snapshot_t) < step / 2:
                fig.savefig(out.with_suffix(".png"), facecolor=BG)
    plt.close(fig)
    return out, res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=3147)
    ap.add_argument("--out", default=str(ROOT / "results" / "clip_day.mp4"))
    ap.add_argument("--step", type=float, default=1.5, help="simulated minutes per frame")
    ap.add_argument("--fps", type=int, default=30)
    a = ap.parse_args()
    out, res = render(a.seed, a.out, step=a.step, fps=a.fps)
    print(out, {k: round(v, 3) for k, v in res.items() if k.startswith("punct")})


if __name__ == "__main__":
    main()
