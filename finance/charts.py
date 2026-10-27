"""
Generazione grafici con Matplotlib
"""

import io
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

from constants import categoria_display, fmt_amount

# Palette colori personalizzata
COLORS = [
    "#FF6B6B", "#4ECDC4", "#45B7D1", "#96CEB4", "#FFEAA7",
    "#DDA0DD", "#98D8C8", "#F7DC6F", "#82E0AA", "#F1948A",
    "#85C1E9", "#F0B27A", "#C39BD3", "#76D7C4", "#F9E79F",
    "#AED6F1",
]

BG_COLOR   = "#1E1E2E"
TEXT_COLOR = "#CDD6F4"
GRID_COLOR = "#313244"
GREEN      = "#A6E3A1"
RED        = "#F38BA8"
BLUE       = "#89B4FA"
YELLOW     = "#F9E2AF"


def _base_style():
    plt.rcParams.update({
        "figure.facecolor": BG_COLOR,
        "axes.facecolor": BG_COLOR,
        "axes.edgecolor": GRID_COLOR,
        "axes.labelcolor": TEXT_COLOR,
        "xtick.color": TEXT_COLOR,
        "ytick.color": TEXT_COLOR,
        "text.color": TEXT_COLOR,
        "grid.color": GRID_COLOR,
        "font.family": "DejaVu Sans",
        "font.size": 10,
    })


def _save_buf(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=130, facecolor=BG_COLOR)
    buf.seek(0)
    plt.close(fig)
    return buf


# ── Torta categorie ─────────────────────────────────────────────

def pie_chart_categorie(rows, title="Spese per categoria"):
    _base_style()
    labels = [categoria_display(r["category"]) for r in rows]
    values = [r["total"] for r in rows]

    fig, ax = plt.subplots(figsize=(9, 6), facecolor=BG_COLOR)
    wedges, texts, autotexts = ax.pie(
        values,
        labels=None,
        autopct=lambda p: f"{p:.1f}%" if p > 4 else "",
        colors=COLORS[:len(values)],
        startangle=140,
        wedgeprops=dict(edgecolor=BG_COLOR, linewidth=2),
        pctdistance=0.75,
    )
    for at in autotexts:
        at.set_color(BG_COLOR)
        at.set_fontweight("bold")
        at.set_fontsize(9)

    # Legenda
    patches = [mpatches.Patch(color=COLORS[i], label=f"{labels[i]}: {fmt_amount(values[i])}")
               for i in range(len(labels))]
    ax.legend(handles=patches, loc="center left", bbox_to_anchor=(1, 0.5),
              frameon=False, fontsize=9)

    total = sum(values)
    ax.text(0, 0, fmt_amount(total), ha="center", va="center",
            fontsize=13, fontweight="bold", color=TEXT_COLOR)

    ax.set_title(title, color=TEXT_COLOR, fontsize=14, pad=16, fontweight="bold")
    fig.tight_layout()
    return _save_buf(fig)


# ── Bar chart trend giornaliero ─────────────────────────────────

def bar_chart_trend(spese_rows, entrate_rows, title="Trend giornaliero"):
    _base_style()

    def to_dict(rows):
        return {r["day"]: r["total"] for r in rows}

    spese_d   = to_dict(spese_rows)
    entrate_d = to_dict(entrate_rows)
    all_days  = sorted(set(list(spese_d.keys()) + list(entrate_d.keys())))

    if not all_days:
        return None

    x      = np.arange(len(all_days))
    spese  = [spese_d.get(d, 0) for d in all_days]
    entrate = [entrate_d.get(d, 0) for d in all_days]
    width  = 0.35

    fig, ax = plt.subplots(figsize=(max(8, len(all_days) * 0.55), 5), facecolor=BG_COLOR)
    ax.bar(x - width/2, spese,   width, label="Spese",   color=RED,   alpha=0.85)
    ax.bar(x + width/2, entrate, width, label="Entrate", color=GREEN, alpha=0.85)

    ax.set_xticks(x)
    labels_x = [d[5:] for d in all_days]  # MM-DD
    ax.set_xticklabels(labels_x, rotation=45, ha="right", fontsize=8)
    ax.yaxis.grid(True, alpha=0.3)
    ax.set_axisbelow(True)
    ax.legend(frameon=False)
    ax.set_title(title, color=TEXT_COLOR, fontsize=13, fontweight="bold")
    fig.tight_layout()
    return _save_buf(fig)


# ── Bar chart budget vs spese ────────────────────────────────────

def bar_budget_comparison(budgets, spent_rows, currency="€"):
    _base_style()
    if not budgets:
        return None

    spent_d = {r["category"]: r["total"] for r in spent_rows}
    cats    = [b["category"] for b in budgets]
    limits  = [b["monthly_limit"] for b in budgets]
    spents  = [spent_d.get(c, 0) for c in cats]
    labels  = [categoria_display(c) for c in cats]

    x     = np.arange(len(cats))
    width = 0.38

    fig, ax = plt.subplots(figsize=(max(7, len(cats) * 1.1), 5), facecolor=BG_COLOR)
    ax.bar(x - width/2, limits, width, label="Budget",  color=BLUE,   alpha=0.75)
    bars_s  = ax.bar(x + width/2, spents, width, label="Speso",   color=RED,    alpha=0.85)

    # Colora in giallo se supera 80%
    for bar, s, l in zip(bars_s, spents, limits):
        if l > 0:
            pct = s / l
            if pct >= 1.0:
                bar.set_color("#FF4444")
            elif pct >= 0.8:
                bar.set_color(YELLOW)

    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=30, ha="right", fontsize=8)
    ax.yaxis.grid(True, alpha=0.3)
    ax.set_axisbelow(True)
    ax.legend(frameon=False)
    ax.set_title("Budget mensile vs Spese", color=TEXT_COLOR, fontsize=13, fontweight="bold")   
    fig.tight_layout()
    return _save_buf(fig)


# ── Sparkline saldo mensile ──────────────────────────────────────

def sparkline_saldo(daily_data, title="Saldo cumulativo"):
    _base_style()
    if not daily_data:
        return None

    days   = [d["day"] for d in daily_data]
    values = list(np.cumsum([d["net"] for d in daily_data]))
    x      = list(range(len(days)))

    fig, ax = plt.subplots(figsize=(9, 4), facecolor=BG_COLOR)
    color_line = GREEN if values[-1] >= 0 else RED
    ax.plot(x, values, color=color_line, linewidth=2.5, zorder=3)
    ax.fill_between(x, values, alpha=0.15, color=color_line)
    ax.axhline(0, color=GRID_COLOR, linewidth=1, linestyle="--", alpha=0.6)

    ax.set_xticks(x[::max(1, len(x)//8)])
    ax.set_xticklabels([days[i][5:] for i in x[::max(1, len(x)//8)]], rotation=45, ha="right", fontsize=8)
    ax.yaxis.grid(True, alpha=0.25)
    ax.set_axisbelow(True)
    ax.set_title(title, color=TEXT_COLOR, fontsize=13, fontweight="bold")
    fig.tight_layout()
    return _save_buf(fig)
