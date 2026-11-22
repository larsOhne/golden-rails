"""Short explainer video of the whole model (German), results/model_explainer.mp4.

Scenes: network, monthly timetable and a simulated day, corridors (blocks, headway, ETCS, priority),
nodes, vehicle circulations and reserves, disruptions and failures, calibration and validation,
KPI and measures. The network map and the simulated day use real model data.

    python -m golden_rails.explainer
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.animation import FFMpegWriter
from matplotlib.patches import FancyBboxPatch, Rectangle

from .network import load_network, place_name
from .sim import DisruptionModel, build_trains, month_timetable, plan_timetable, simulate_day

ROOT = Path(__file__).resolve().parent.parent
RES = ROOT / "results"
BG, PANEL, NET, INK, INK2, MUTED = "#0d0d0d", "#1a1a19", "#3a3a37", "#f0efec", "#c3c2b7", "#8a897f"
BLUE, ORANGE, GREEN, RED, YELLOW, GREY = "#3b8eea", "#eb6834", "#1baf7a", "#d03b3b", "#fab219", "#7d7c75"
TYPE_COL = {"FV": BLUE, "NV": ORANGE, "S": GREEN, "GV": GREY}
FPS = 15
W, H = 12.8, 7.2


def ease(x):
    x = min(max(x, 0.0), 1.0)
    return x * x * (3 - 2 * x)


class Ctx:
    """Data shared by the scenes."""

    def __init__(self):
        self.net = load_network()
        tt, bg = month_timetable("2026-05")
        self.net.bg_file = str(bg)
        self.trains = build_trains(self.net, tt_file=tt)
        plan_timetable(self.net, self.trains)
        trace = []
        simulate_day(self.net, self.trains, DisruptionModel(), 4242, trace=trace)
        n = self.net.nodes
        lon, lat = n.lon.to_dict(), n.lat.to_dict()
        segs = [self.net.segments[t[3]] for t in trace]
        self.te = np.array([t[1] for t in trace])
        self.tx = np.array([t[2] for t in trace])
        d = np.array([t[5] for t in trace], float)
        blk = np.array([t[4] for t in trace], float)
        nb = np.array([s.n_blocks() for s in segs], float)
        ax_ = np.array([lon[s.a] for s in segs]); ay_ = np.array([lat[s.a] for s in segs])
        bx_ = np.array([lon[s.b] for s in segs]); by_ = np.array([lat[s.b] for s in segs])
        # block start/end fractions along the corridor in driving direction
        f0 = np.where(d > 0, blk / nb, (blk + 1) / nb)
        f1 = np.where(d > 0, (blk + 1) / nb, blk / nb)
        self.x0, self.y0 = ax_ + (bx_ - ax_) * f0, ay_ + (by_ - ay_) * f0
        self.x1, self.y1 = ax_ + (bx_ - ax_) * f1, ay_ + (by_ - ay_) * f1
        self.dly = np.nan_to_num(np.array([t[6] for t in trace], float))
        self.col = np.array([TYPE_COL[self.trains[t[0]].ttype] for t in trace])
        self.size = np.array([22 if self.trains[t[0]].ttype == "FV" else 7 for t in trace])
        self.n_trains = len(self.trains)
        self.n_groups = len({t.group for t in self.trains})
        self.val = json.loads((RES / "validation_val.json").read_text())

    def positions(self, t):
        m = (self.te <= t) & (self.tx > t)
        f = (t - self.te[m]) / np.maximum(self.tx[m] - self.te[m], 1e-6)
        return (self.x0[m] + (self.x1[m] - self.x0[m]) * f, self.y0[m] + (self.y1[m] - self.y0[m]) * f,
                self.col[m], self.size[m], self.dly[m])


# ---------------------------------------------------------------- drawing helpers
def header(fig, num, title, sub=""):
    fig.text(0.04, 0.92, f"{num}", color=YELLOW, fontsize=15, fontweight="bold")
    fig.text(0.075, 0.92, title, color=INK, fontsize=22, fontweight="bold")
    if sub:
        fig.text(0.075, 0.875, sub, color=INK2, fontsize=12.5)


def bullets(fig, items, x, y, t, start=0.3, step=0.12, size=13, dy=0.065):
    for k, it in enumerate(items):
        a = ease((t - start - k * step) / 0.12)
        if a > 0:
            fig.text(x, y - k * dy, "•  " + it, color=INK, fontsize=size, alpha=a, va="top")


def map_axes(fig, rect=(0.0, 0.0, 0.58, 0.86)):
    ax = fig.add_axes(rect, facecolor=BG)
    ax.set_axis_off()
    ax.set_xlim(5.6, 15.4)
    ax.set_ylim(47.3, 54.2)
    ax.set_aspect(1.55)
    return ax


def draw_net(ax, net, frac=1.0, color=NET):
    n = net.nodes
    segs = sorted(net.segments.values(), key=lambda s: (n.lat[s.a] + n.lat[s.b]))
    k = int(len(segs) * frac)
    for s in segs[:k]:
        ax.plot([n.lon[s.a], n.lon[s.b]], [n.lat[s.a], n.lat[s.b]], color=color, lw=0.6 + 1.0 * s.tracks,
                solid_capstyle="round", zorder=1)


# ---------------------------------------------------------------- scenes
def s_title(fig, c, t):
    a = ease(t / 0.3)
    try:
        img = plt.imread(ROOT / "assets" / "logo.png")
        ax = fig.add_axes([0.40, 0.42, 0.2, 0.42])
        ax.imshow(img, alpha=a)
        ax.set_axis_off()
    except FileNotFoundError:
        pass
    fig.text(0.5, 0.31, "golden-rails", color=INK, fontsize=34, fontweight="bold", ha="center", alpha=a)
    fig.text(0.5, 0.24, "Wie das Modell funktioniert", color=INK2, fontsize=18, ha="center", alpha=a)
    fig.text(0.5, 0.15, "Wo bringt ein Euro im deutschen Bahnnetz die meiste Pünktlichkeit?",
             color=YELLOW, fontsize=14, ha="center", alpha=ease((t - 0.4) / 0.3))


def s_network(fig, c, t):
    header(fig, "1", "Das Netz", "Korridore zwischen Knoten, aus OpenStreetMap")
    ax = map_axes(fig)
    draw_net(ax, c.net, ease(t / 0.6))
    if t > 0.5:
        n = c.net.nodes
        for nid, r in n.iterrows():
            if r.major:
                ax.plot(r.lon, r.lat, "o", color=INK, ms=4, zorder=3)
                ax.annotate(place_name(n, nid), (r.lon, r.lat), xytext=(3, 3), textcoords="offset points",
                            color=MUTED, fontsize=7.5)
    bullets(fig, [f"{len(c.net.segments)} Korridore, {len(c.net.nodes)} Knoten",
                  "Linienbreite = Zahl der Gleise (OSM, Engstelle)",
                  "Länge, Geschwindigkeit, ETCS-Ausrüstung aus OSM",
                  "Jeder Korridor ist in Blöcke geteilt,",
                  "   je Gleis ein Kanal pro Richtung"], 0.6, 0.72, t, start=0.15, step=0.12)


def s_day(fig, c, t):
    header(fig, "2", "Fahrplan und ein simulierter Tag", "Monatsfahrplan aus den DB-Plandaten (hier Mai 2026)")
    ax = map_axes(fig)
    draw_net(ax, c.net)
    tm = 5 * 60 + t * 19 * 60
    x, y, col, sz, dl = c.positions(tm)
    edge = np.where(dl >= 15, RED, np.where(dl >= 6, YELLOW, "none"))
    ax.scatter(x, y, s=sz, c=col, edgecolors=edge, linewidths=0.9, zorder=4)
    fig.text(0.47, 0.05, f"{int(tm // 60):02d}:{int(tm % 60):02d}", color=INK, fontsize=22,
             fontweight="bold", family="monospace")
    bullets(fig, [f"{c.n_trains:,} Züge pro Tag".replace(",", "."),
                  "ICE/IC, RE/RB, S-Bahn (wo auf Fernbahngleisen), Güter",
                  "12 Monatsfahrpläne: echte Umleitungen und Sperrungen",
                  "Konfliktfreie Planung nach Priorität",
                  "ICE > RE > S-Bahn > Güterzug"], 0.6, 0.72, t, start=0.05, step=0.1)
    for k, (lab, cc) in enumerate([("ICE/IC", BLUE), ("RE/RB", ORANGE), ("S-Bahn", GREEN), ("Güter", GREY)]):
        fig.text(0.61 + k * 0.085, 0.3, "●", color=cc, fontsize=14)
        fig.text(0.63 + k * 0.085, 0.302, lab, color=INK2, fontsize=10)
    fig.text(0.61, 0.25, "gelber / roter Rand: ≥ 6 / ≥ 15 min verspätet", color=MUTED, fontsize=10)


def s_corridor(fig, c, t):
    header(fig, "3", "Korridore: Blöcke, Zugfolge, ETCS", "Mesoskopisch: wer darf wann in welchen Block?")
    ax = fig.add_axes([0.05, 0.25, 0.9, 0.5], facecolor=BG)
    ax.set_axis_off()
    ax.set_xlim(0, 10)
    ax.set_ylim(-1.6, 2.4)
    etcs = t > 0.5
    for y in (1.0, 0.0):
        ax.plot([0, 10], [y, y], color=NET, lw=6, solid_capstyle="round")
    for xb in np.arange(0, 10.01, 2.5):
        ax.plot([xb, xb], [-0.25, 1.25], color=MUTED, lw=1, ls=":")
    ax.text(0, 1.45, "Gleis Richtung Ost", color=MUTED, fontsize=10)
    ax.text(0, -0.55, "Gleis Richtung West", color=MUTED, fontsize=10)
    gap = 1.25 if not etcs else 0.8
    tl = (t % 0.5) / 0.5
    base = -1.5 + tl * 11
    for k, (col, lab) in enumerate([(BLUE, "ICE"), (BLUE if etcs else GREY, "ICE" if etcs else "IC"),
                                    (ORANGE, "RE")]):
        x0 = base - k * (gap + 0.9)
        if -1 < x0 < 10.5:
            ax.add_patch(FancyBboxPatch((x0, 0.82), 0.9, 0.36, boxstyle="round,pad=0.02", color=col))
            ax.text(x0 + 0.45, 1.0, lab, color="white", fontsize=9, ha="center", va="center", fontweight="bold")
    xg = 9.5 - tl * 11
    if -1 < xg < 10.5:
        ax.add_patch(Rectangle((xg, -0.18), 1.6, 0.36, color=GREY))
        ax.text(xg + 0.8, 0.0, "Güterzug", color="white", fontsize=9, ha="center", va="center")
    lab = ("ETCS: 2 min Zugfolge, aber nur für ausgerüstete Züge" if etcs
           else "Lichtsignale: 3 min Zugfolge, kein Überholen im Block")
    ax.text(5, 2.0, lab, color=GREEN if etcs else INK, fontsize=15, ha="center", fontweight="bold")
    bullets(fig, ["Mischverkehr: nicht ausgerüsteter Zug fährt mit 3 min (38 Flottengruppen)",
                  "Nur-ETCS-Strecken: nur ausgerüstete Züge (Flotte nachrüsten)",
                  "Fahrdienstleiter schaut 15 min voraus: hält rangniedrigere Züge",
                  "   zurück und bewahrt die geplante Reihenfolge"], 0.08, 0.22, t, start=0.1, step=0.15,
            size=12, dy=0.05)


def s_node(fig, c, t):
    header(fig, "4", "Knoten: Bahnsteige und Einfahrt", "44 große Bahnhöfe mit begrenzter Kapazität")
    ax = fig.add_axes([0.04, 0.12, 0.56, 0.68], facecolor=BG)
    ax.set_axis_off()
    ax.set_xlim(0, 12)
    ax.set_ylim(-0.5, 5)
    ys = [0.8, 1.8, 2.8, 3.8]
    for y in ys:
        ax.plot([4, 8], [y, y], color=NET, lw=6)
        ax.plot([2, 4], [2.3, y], color=NET, lw=2)
        ax.plot([8, 10], [y, 2.3], color=NET, lw=2)
    ax.plot([0, 2], [2.3, 2.3], color=NET, lw=6)
    ax.plot([10, 12], [2.3, 2.3], color=NET, lw=6)
    ax.add_patch(Rectangle((1.6, 1.9), 0.8, 0.8, fill=False, ec=YELLOW, lw=1.5, ls="--"))
    ax.text(2.0, 3.0, "Einfahrt\n(Fahrstraße)", color=YELLOW, fontsize=9, ha="center")
    ax.add_patch(FancyBboxPatch((4.6, ys[3] - 0.2), 2.6, 0.4, boxstyle="round,pad=0.02", color=GREY))
    ax.text(5.9, ys[3], "Hintergrundzug", color="white", fontsize=8.5, ha="center", va="center")
    # two trains: one arrives, dwells, leaves; second waits for the throat
    def pos(tt, y):
        if tt < 0.3:
            return 0 + tt / 0.3 * 4.4, 2.3 + (y - 2.3) * max(0, (tt / 0.3 * 4.4 - 2) / 2.4)
        if tt < 0.6:
            return 4.4, y
        x = 4.4 + (tt - 0.6) / 0.4 * 7.6
        return x, y + (2.3 - y) * min(1, max(0, (x - 8) / 2))
    for k, (col, y, lag) in enumerate([(BLUE, ys[1], 0.0), (ORANGE, ys[0], 0.18)]):
        tt = (t - lag) / 0.82
        if 0 <= tt <= 1:
            x, yy = pos(tt, y)
            ax.add_patch(FancyBboxPatch((x - 0.9, yy - 0.18), 1.8, 0.36, boxstyle="round,pad=0.02", color=col))
    bullets(fig, ["Bahnsteiggleise je Knoten (Gleispläne)",
                  "Jede Ein- und Ausfahrt belegt eine Fahrstraße 1,5 min",
                  "Züge, die nur diesen Knoten berühren, belegen",
                  "   Gleise mit gemessener Verspätung",
                  "Maßnahmen: +Bahnsteig, Überwerfung, ETCS im Knoten"], 0.62, 0.74, t, start=0.1, step=0.13,
            size=12.5)


def s_circulation(fig, c, t):
    header(fig, "5", "Fahrzeugumläufe und Reserven", "Verspätung wandert mit dem Fahrzeug in die nächste Fahrt")
    ax = fig.add_axes([0.06, 0.38, 0.88, 0.42], facecolor=BG)
    ax.set_axis_off()
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 4)
    late = 18 * ease(t / 0.35)
    ax.add_patch(Rectangle((5, 2.6), 35 + late, 0.7, color=BLUE))
    ax.text(6, 2.95, "Fahrt A (verspätet)", color="white", fontsize=10, va="center")
    ax.add_patch(Rectangle((40, 2.4), 8, 1.1, fill=False, ec=MUTED, ls=":"))
    ax.text(44, 3.65, "Wendepuffer", color=MUTED, fontsize=9, ha="center")
    reserve = t > 0.6
    b0 = 50 if reserve else max(50, 40 + late + 5)
    ax.add_patch(Rectangle((b0, 2.6), 35, 0.7, color=GREEN if reserve else ORANGE))
    ax.text(b0 + 1, 2.95, "Fahrt B" + (" mit Reservezug pünktlich" if reserve else " startet verspätet"),
            color="white", fontsize=10, va="center")
    if reserve:
        ax.text(50, 1.9, "Reservezug am Knoten übernimmt, Fahrzeug A wird zur Reserve", color=GREEN, fontsize=11)
    ax.plot([0, 100], [2.2, 2.2], color=NET, lw=1)
    bullets(fig, ["Umläufe aus dem Fahrplan abgeleitet (gleiche Linie, Mindestwendezeit)",
                  "Kalibriert: Folgezug ≥ 6 min verspätet, wenn Zug 5–10 min zu spät kam:",
                  "   gemessen 16 %, Modell 16 % (Regionalverkehr)",
                  "Fahrzeugausfall vor Abfahrt: Reservezug springt ein oder Zug fällt aus"],
            0.08, 0.34, t, start=0.15, step=0.13, size=12.5, dy=0.06)


def s_disruptions(fig, c, t):
    header(fig, "6", "Störungen und Ausfälle", "Standardstörungen, Monte-Carlo über viele Tage")
    items = [("Startverspätung beim Eintritt ins Modell", "gemessene Verteilung je Knoten"),
             ("Verlängerte Haltezeiten", "zufällig, je Zuggattung"),
             ("Gleisstörung", "ein Gleis gesperrt, Gegengleisbetrieb"),
             ("Vollsperrung", "Fern- und Güterzüge werden umgeleitet"),
             ("Fahrzeugausfall unterwegs", "Zug blockiert das Gleis, Rest der Fahrt fällt aus"),
             ("Ausfall", "Fahrgäste warten auf den nächsten Zug der Linie")]
    for k, (a, b) in enumerate(items):
        al = ease((t - 0.05 - k * 0.11) / 0.1)
        if al > 0:
            y = 0.76 - k * 0.1
            fig.text(0.07, y, "●", color=[YELLOW, YELLOW, RED, RED, ORANGE, ORANGE][k], fontsize=16, alpha=al)
            fig.text(0.1, y, a, color=INK, fontsize=15, alpha=al)
            fig.text(0.1, y - 0.035, b, color=MUTED, fontsize=11.5, alpha=al)
    ax = fig.add_axes([0.58, 0.32, 0.38, 0.3], facecolor=BG)
    ax.set_axis_off()
    ax.set_xlim(0, 10)
    ax.set_ylim(-1, 2)
    ax.plot([0, 10], [0.5, 0.5], color=NET, lw=6)
    ax.add_patch(FancyBboxPatch((5.2, 0.32), 1.6, 0.36, boxstyle="round,pad=0.02", color=RED))
    ax.text(6.0, 0.95, "✕ liegengeblieben", color=RED, fontsize=11, ha="center")
    xf = min(3.2, 0.2 + t * 6)
    ax.add_patch(FancyBboxPatch((xf, 0.32), 1.6, 0.36, boxstyle="round,pad=0.02", color=BLUE))
    ax.text(5, -0.6, "Folgezug wartet", color=INK2, fontsize=11, ha="center")
    fig.text(0.07, 0.1, "Alle Raten an den DB-Daten kalibriert (Ausfallquoten: Fahrzeuganteil 40 %, Annahme)",
             color=INK2, fontsize=11.5)


def s_validation(fig, c, t):
    header(fig, "7", "Kalibriert und geprüft an einem Jahr DB-Daten",
           "Kalibriert auf 6 geraden Monaten, geprüft auf 6 ungeraden (Nov 2025 – Okt 2026)")
    v = c.val
    rows = [("Pünktlichkeit je Bahnhof, Fernverkehr", v["nodes_FV"]["corr"], 0.94, 0.62),
            ("Pünktlichkeit je Bahnhof, Regionalverkehr", v["nodes_NV"]["corr"], 0.96, 0.41),
            ("Verspätungszuwachs zw. Bahnhöfen, Fernverkehr", v["legs_FV"]["corr"], 0.95, 0.31),
            ("Verspätungszuwachs zw. Bahnhöfen, Regionalverkehr", v["legs_NV"]["corr"], None, None)]
    ax = fig.add_axes([0.42, 0.24, 0.53, 0.56], facecolor=BG)
    ax.set_xlim(0, 1)
    ax.set_ylim(-0.6, len(rows) - 0.4)
    ax.invert_yaxis()
    for sp in ax.spines.values():
        sp.set_visible(False)
    ax.set_yticks([])
    ax.tick_params(colors=MUTED, labelsize=9)
    g = ease(t / 0.5)
    for k, (lab, val, ceil, old) in enumerate(rows):
        if ceil:
            ax.barh(k, ceil, height=0.62, color=PANEL, edgecolor=NET)
        if old:
            ax.barh(k - 0.17, old * g, height=0.22, color=GREY)
        ax.barh(k + 0.1, val * g, height=0.34, color=BLUE)
        ax.text(val * g + 0.01, k + 0.1, f"r = {val:.2f}".replace(".", ","), color=INK, fontsize=11, va="center")
        fig.text(0.04, 0.765 - k * 0.137, lab, color=INK, fontsize=12.5)
    fig.text(0.42, 0.17, "■ Modell (Prüfmonate)   ■ erste Runde   □ Obergrenze der Messdaten",
             color=INK2, fontsize=11)
    nat = v["national_sim"], v["national_obs"]
    fig.text(0.04, 0.17, f"Pünktlichkeit Fernverkehr: Modell {nat[0]['FV'] * 100:.0f} %, gemessen "
                         f"{nat[1]['FV'] * 100:.0f} %", color=INK2, fontsize=11)
    fig.text(0.04, 0.12, "Schwach: Monat-zu-Monat-Schwankungen (Baustellen, Wetter)", color=MUTED, fontsize=11)


def s_kpi(fig, c, t):
    header(fig, "8", "Kennzahl und Maßnahmen", "Nächster Schritt: Bewertung über alle 12 Monatsfahrpläne")
    a = ease(t / 0.25)
    fig.text(0.5, 0.7, "eingesparte Fahrgast-Verspätungsminuten pro Jahr", color=INK, fontsize=19,
             ha="center", alpha=a)
    fig.text(0.5, 0.645, "─" * 44, color=YELLOW, fontsize=16, ha="center", alpha=a)
    fig.text(0.5, 0.59, "Jahreskosten (Annuität der Investition + Betrieb)", color=INK, fontsize=19,
             ha="center", alpha=a)
    fig.text(0.5, 0.52, "Ausfälle zählen mit: Wartezeit auf den nächsten Zug der Linie", color=MUTED,
             fontsize=12, ha="center", alpha=a)
    ms = [("ETCS je Abschnitt", "mit Signalen oder Nur-ETCS"), ("Flotte", "ETCS-Nachrüstung, Reservezüge"),
          ("Neue Gleise", "+1 / +2 im Korridor"), ("Knoten", "Bahnsteig, Überwerfung, ETCS"),
          ("Fahrplan", "±3 min, gleiche Ankünfte je Stunde")]
    for k, (m, d) in enumerate(ms):
        al = ease((t - 0.3 - k * 0.1) / 0.1)
        if al > 0:
            x = 0.06 + k * 0.185
            fig.patches.append(FancyBboxPatch((x, 0.2), 0.165, 0.2, boxstyle="round,pad=0.01",
                                              transform=fig.transFigure, color=PANEL, alpha=al))
            fig.text(x + 0.0825, 0.33, m, color=YELLOW, fontsize=13.5, ha="center", fontweight="bold", alpha=al)
            fig.text(x + 0.0825, 0.26, d, color=INK2, fontsize=10, ha="center", alpha=al, wrap=True)


SCENES = [(s_title, 4.5), (s_network, 8), (s_day, 11), (s_corridor, 10), (s_node, 9), (s_circulation, 9),
          (s_disruptions, 9), (s_validation, 10), (s_kpi, 8)]


def render(out=RES / "model_explainer.mp4"):
    c = Ctx()
    fig = plt.figure(figsize=(W, H), dpi=100, facecolor=BG)
    writer = FFMpegWriter(fps=FPS, bitrate=2500)
    with writer.saving(fig, str(out), dpi=100):
        for scene, dur in SCENES:
            n = int(dur * FPS)
            for k in range(n):
                fig.clf()
                fig.set_facecolor(BG)
                t = k / (n - 1)
                scene(fig, c, t)
                fade = min(1.0, k / 6, (n - 1 - k) / 6)
                if fade < 1:
                    fig.patches.append(Rectangle((0, 0), 1, 1, transform=fig.transFigure, color=BG,
                                                 alpha=1 - fade, zorder=100))
                writer.grab_frame()
    # poster frames
    for scene, name in ((s_day, "explainer_day.png"), (s_validation, "explainer_validation.png")):
        fig.clf()
        fig.set_facecolor(BG)
        scene(fig, c, 0.55 if scene is s_day else 1.0)
        fig.savefig(RES / name, facecolor=BG)
    plt.close(fig)
    return out


if __name__ == "__main__":
    print(render())
