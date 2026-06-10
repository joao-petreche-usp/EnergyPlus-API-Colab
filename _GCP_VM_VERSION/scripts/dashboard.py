"""
dashboard.py -- EnergyPlus Pareto Optimization Dashboard
=========================================================
Plotly/Dash Dashboard for visualization of optimization results.

Usage:
    pip install dash plotly

    # With JSONs downloaded from GCS
    python dashboard.py --pareto data/pareto_front.json --gsa gsa_results_20260518.json

    # Without JSONs (uses embedded data from the real 2026-05-18 run)
    python dashboard.py

    # Static HTML (no server)
    python dashboard.py --static
    python dashboard.py --static --output data/report.html

Download the JSONs from GCS beforehand:
    gcloud storage cp gs://eplus-colab-cloud-data/results/gcp_vm_pareto_20260518_232147/pareto_front.json data/
    gcloud storage cp plan/gsa_results_20260518.json .
"""

import argparse
import json
from pathlib import Path

# -- Exact data from the 2026-05-18 run (48 EnergyPlus simulations) -----------
# Accepts int (0,90,180,270) and float (0.0,...) — Real JSON uses int
_ORIENT_LABELS = {
    0: "North",
    90: "East",
    180: "South",
    270: "West",
    0.0: "North",
    90.0: "East",
    180.0: "South",
    270.0: "West",
}


def _c(o, s, b, wr, rr, hvac):
    return {
        "candidate_id": f"MLT_o{o}_s{s}_b{b}",
        "orientation": [0.0, 90.0, 180.0, 270.0][o],
        "orient_label": ["North", "East", "South", "West"][o],
        "setpoint": s,
        "budget": b,
        "wall_r": wr,
        "roof_r": rr,
        "glass_u": 1.0,
        "fr1_proxy": round(17.072 + (s - 23) * 0.544, 3),
        "fr2_proxy": round(
            -0.3 + o * 0.05 - [0, 1, 2, 3].index([30, 40, 50, 65].index(b)) * 0.08, 3
        ),
        "hvac_energy_kwh": hvac,
    }


_CANDIDATES_EXACT = [
    # orient=0 (North 0 degrees)
    _c(0, 23, 30, 1.5, 1.5, 67694.50),
    _c(0, 23, 40, 2.0, 2.0, 65272.27),
    _c(0, 23, 50, 2.5, 2.5, 63750.05),
    _c(0, 23, 65, 2.5, 4.0, 62136.16),
    _c(0, 24, 30, 1.5, 1.5, 66980.61),
    _c(0, 24, 40, 2.0, 2.0, 64655.61),
    _c(0, 24, 50, 2.5, 2.5, 63197.27),
    _c(0, 24, 65, 2.5, 4.0, 61727.83),
    _c(0, 25, 30, 1.5, 1.5, 66327.83),
    _c(0, 25, 40, 2.0, 2.0, 64058.38),
    _c(0, 25, 50, 2.5, 2.5, 62650.05),
    _c(0, 25, 65, 2.5, 4.0, 61258.38),
    # orient=1 (East 90 degrees)
    _c(1, 23, 30, 1.5, 1.5, 68580.61),
    _c(1, 23, 40, 2.0, 2.0, 66063.94),
    _c(1, 23, 50, 2.5, 2.5, 64466.72),
    _c(1, 23, 65, 2.5, 4.0, 62830.61),
    _c(1, 24, 30, 1.5, 1.5, 67788.94),
    _c(1, 24, 40, 2.0, 2.0, 65358.39),
    _c(1, 24, 50, 2.5, 2.5, 63830.61),
    _c(1, 24, 65, 2.5, 4.0, 62316.72),
    _c(1, 25, 30, 1.5, 1.5, 67130.61),
    _c(1, 25, 40, 2.0, 2.0, 64733.39),
    _c(1, 25, 50, 2.5, 2.5, 63236.16),
    _c(1, 25, 65, 2.5, 4.0, 61775.05),
    # orient=2 (South 180 degrees)
    _c(2, 23, 30, 1.5, 1.5, 68230.61),
    _c(2, 23, 40, 2.0, 2.0, 65738.94),
    _c(2, 23, 50, 2.5, 2.5, 64163.94),
    _c(2, 23, 65, 2.5, 4.0, 62508.38),
    _c(2, 24, 30, 1.5, 1.5, 67441.72),
    _c(2, 24, 40, 2.0, 2.0, 65030.61),
    _c(2, 24, 50, 2.5, 2.5, 63516.72),
    _c(2, 24, 65, 2.5, 4.0, 61988.94),
    _c(2, 25, 30, 1.5, 1.5, 66788.94),
    _c(2, 25, 40, 2.0, 2.0, 64425.05),
    _c(2, 25, 50, 2.5, 2.5, 62955.61),
    _c(2, 25, 65, 2.5, 4.0, 61497.27),
    # orient=3 (West 270 degrees)
    _c(3, 23, 30, 1.5, 1.5, 68594.50),
    _c(3, 23, 40, 2.0, 2.0, 66091.72),
    _c(3, 23, 50, 2.5, 2.5, 64505.61),
    _c(3, 23, 65, 2.5, 4.0, 62825.05),
    _c(3, 24, 30, 1.5, 1.5, 67783.39),
    _c(3, 24, 40, 2.0, 2.0, 65369.50),
    _c(3, 24, 50, 2.5, 2.5, 63847.27),
    _c(3, 24, 65, 2.5, 4.0, 62297.27),
    _c(3, 25, 30, 1.5, 1.5, 67091.72),
    _c(3, 25, 40, 2.0, 2.0, 64708.39),
    _c(3, 25, 50, 2.5, 2.5, 63222.27),
    _c(3, 25, 65, 2.5, 4.0, 61730.60),
]

_PARETO_FALLBACK = {
    "all_candidates": _CANDIDATES_EXACT,
    "level2_filtered": _CANDIDATES_EXACT,
    "pareto_front": [
        {
            "candidate_id": "MLT_o0_s23_b65",
            "orientation": 0.0,
            "orient_label": "North",
            "setpoint": 23,
            "wall_r": 2.5,
            "roof_r": 4.0,
            "budget": 65,
            "fr1_proxy": 17.072,
            "hvac_energy_kwh": 62136.16,
        },
        {
            "candidate_id": "MLT_o0_s24_b65",
            "orientation": 0.0,
            "orient_label": "North",
            "setpoint": 24,
            "wall_r": 2.5,
            "roof_r": 4.0,
            "budget": 65,
            "fr1_proxy": 17.616,
            "hvac_energy_kwh": 61727.83,
        },
        {
            "candidate_id": "MLT_o0_s25_b65",
            "orientation": 0.0,
            "orient_label": "North",
            "setpoint": 25,
            "wall_r": 2.5,
            "roof_r": 4.0,
            "budget": 65,
            "fr1_proxy": 18.160,
            "hvac_energy_kwh": 61258.38,
        },
    ],
    "fr1_opt": 17.072,
    "epsilon": 1.632,
}

_GSA_FALLBACK = {
    "mu_star": {
        "wall_r": 2646.1,
        "roof_r": 2548.6,
        "glass_u": 1906.4,
        "orientation_idx": 4548.9,
        "setpoint": 2274.2,
    },
    "sigma": {
        "wall_r": 3854.3,
        "roof_r": 3570.5,
        "glass_u": 3326.7,
        "orientation_idx": 6016.9,
        "setpoint": 3654.7,
    },
    "ranking": ["orientation_idx", "wall_r", "roof_r", "setpoint", "glass_u"],
    "n_valid": 90,
}

DP_LABELS = {
    "orientation_idx": "Orientation",
    "wall_r": "Wall Insul.",
    "roof_r": "Roof Insul.",
    "setpoint": "HVAC Setpoint",
    "glass_u": "Glazing U-value",
}

# -- Palette ------------------------------------------------------------------
C = {
    "bg": "#0f1117",
    "surface": "#1a1d27",
    "border": "#2a2d3a",
    "text": "#e8eaf0",
    "muted": "#6b7280",
    "green": "#4ade80",
    "blue": "#60a5fa",
    "amber": "#f59e0b",
    "pink": "#f472b6",
    "red": "#f87171",
    "yellow": "#fbbf24",
}

ORIENT_COLOR = {
    "North": C["green"],
    "East": C["red"],
    "South": C["blue"],
    "West": C["yellow"],
}


# -- Loading and data normalization -------------------------------------------
def load_data(pareto_path: str | None, gsa_path: str | None) -> tuple[dict, dict]:
    pareto = _PARETO_FALLBACK
    gsa = _GSA_FALLBACK

    if pareto_path and Path(pareto_path).exists():
        with open(pareto_path, encoding="utf-8") as f:
            pareto = json.load(f)
        pareto = _normalize_pareto(pareto)
        print(f"Pareto loaded: {pareto_path}")
    else:
        print(
            "pareto_front.json not found -- using embedded data from the 2026-05-18 run"
        )

    if gsa_path and Path(gsa_path).exists():
        with open(gsa_path, encoding="utf-8") as f:
            gsa = json.load(f)
        print(f"GSA loaded: {gsa_path}")
    else:
        print("gsa_results.json not found -- using Morris N=15 results")

    return pareto, gsa


def _normalize_pareto(pareto: dict) -> dict:
    """Ensures orient_label in all candidates and propagates hvac_energy_kwh."""
    energy_by_id = {
        c["candidate_id"]: c.get("hvac_energy_kwh")
        for c in pareto.get("level2_filtered", [])
        if c.get("hvac_energy_kwh") is not None
    }
    for lst in ("all_candidates", "level2_filtered", "pareto_front"):
        for c in pareto.get(lst, []):
            if "orient_label" not in c:
                # orientation can be int (0,90,180,270) or float — both in dict
                orient_raw = c.get("orientation", 0)
                c["orient_label"] = _ORIENT_LABELS.get(
                    orient_raw
                ) or _ORIENT_LABELS.get(int(orient_raw), "?")
            if "hvac_energy_kwh" not in c:
                c["hvac_energy_kwh"] = energy_by_id.get(c["candidate_id"])
    return pareto


# -- Layout helpers -----------------------------------------------------------
def _layout(**kw) -> dict:
    base = dict(
        paper_bgcolor=C["bg"],
        plot_bgcolor=C["surface"],
        font=dict(family="'IBM Plex Mono', monospace", color=C["text"], size=12),
        margin=dict(l=52, r=20, t=48, b=48),
        xaxis=dict(gridcolor=C["border"], zerolinecolor=C["border"]),
        yaxis=dict(gridcolor=C["border"], zerolinecolor=C["border"]),
    )
    base.update(kw)
    return base


def _title(text: str) -> dict:
    return dict(text=text, font=dict(size=13, color=C["text"]))


def _legend() -> dict:
    return dict(
        bgcolor=C["surface"], bordercolor=C["border"], borderwidth=1, font=dict(size=11)
    )


# -- Figure 1: Pareto Front --------------------------------------------------
def fig_pareto_front(pareto: dict):
    import plotly.graph_objects as go

    source = pareto.get("level2_filtered") or pareto.get("all_candidates", [])
    cands = [c for c in source if c.get("hvac_energy_kwh") is not None]
    pareto_pts = pareto.get("pareto_front", [])
    pareto_ids = {p["candidate_id"] for p in pareto_pts}

    fig = go.Figure()

    groups: dict[str, list] = {}
    for c in cands:
        groups.setdefault(c["orient_label"], []).append(c)

    for lbl, cs in groups.items():
        non_pareto = [c for c in cs if c["candidate_id"] not in pareto_ids]
        if non_pareto:
            fig.add_trace(
                go.Scatter(
                    x=[c["fr1_proxy"] for c in non_pareto],
                    y=[c["hvac_energy_kwh"] for c in non_pareto],
                    mode="markers",
                    name=lbl,
                    legendgroup=lbl,
                    text=[
                        f"{c['candidate_id']}<br>"
                        f"Setpoint {c['setpoint']}C | {lbl}<br>"
                        f"wall={c['wall_r']} roof={c['roof_r']}<br>"
                        f"HVAC: {c['hvac_energy_kwh']:,.0f} kWh"
                        for c in non_pareto
                    ],
                    hovertemplate="%{text}<extra></extra>",
                    marker=dict(
                        color=ORIENT_COLOR.get(lbl, C["muted"]),
                        size=7,
                        opacity=0.45,
                        line=dict(width=0),
                    ),
                )
            )

    px_ = [p["fr1_proxy"] for p in pareto_pts]
    py_ = [p["hvac_energy_kwh"] for p in pareto_pts]
    if px_:
        fig.add_trace(
            go.Scatter(
                x=px_,
                y=py_,
                mode="lines",
                line=dict(color=C["pink"], width=1.5, dash="dot"),
                showlegend=False,
                hoverinfo="skip",
            )
        )
        fig.add_trace(
            go.Scatter(
                x=px_,
                y=py_,
                mode="markers+text",
                name="Pareto Front",
                legendgroup="pareto",
                text=[f"S{p['setpoint']}" for p in pareto_pts],
                textposition="top right",
                textfont=dict(color=C["pink"], size=11),
                customdata=[
                    f"PARETO -- {p['candidate_id']}<br>"
                    f"Setpoint: {p['setpoint']}C | {p.get('orient_label', '')} <br>"
                    f"wall_r={p['wall_r']} | roof_r={p['roof_r']}<br>"
                    f"FR1 (conforto): {p['fr1_proxy']:.3f}<br>"
                    f"HVAC: {p['hvac_energy_kwh']:,.0f} kWh"
                    for p in pareto_pts
                ],
                hovertemplate="%{customdata}<extra></extra>",
                marker=dict(
                    color=C["pink"],
                    size=14,
                    symbol="star",
                    line=dict(color="white", width=1.5),
                ),
            )
        )

    fig.update_layout(
        **_layout(
            title=_title("Pareto Front -- FR1 (Comfort) x FR2 (HVAC Energy)"),
            xaxis_title="FR1 proxy -- comfort (higher = worse)",
            yaxis_title="Annual HVAC energy (kWh)",
            legend=_legend(),
        )
    )
    return fig


# -- Figure 2: Average Energy x Budget by Orientation --------------------------
def fig_energy_by_budget(pareto: dict):
    import plotly.graph_objects as go

    source = pareto.get("level2_filtered") or pareto.get("all_candidates", [])
    cands = [c for c in source if c.get("hvac_energy_kwh") is not None]
    if not cands:
        return go.Figure()

    fig = go.Figure()

    groups: dict[str, list] = {}
    for c in cands:
        groups.setdefault(c["orient_label"], []).append(c)

    for lbl, cs in groups.items():
        budget_map: dict[int, list] = {}
        for c in cs:
            b = c.get("budget", int(c["wall_r"] * 10 + c["roof_r"] * 10))
            budget_map.setdefault(b, []).append(c["hvac_energy_kwh"])

        x = sorted(budget_map)
        y = [sum(budget_map[b]) / len(budget_map[b]) for b in x]
        fig.add_trace(
            go.Scatter(
                x=x,
                y=y,
                mode="lines+markers",
                name=lbl,
                legendgroup=lbl,
                line=dict(color=ORIENT_COLOR.get(lbl, C["muted"]), width=2),
                marker=dict(size=8),
                hovertemplate=f"{lbl}<br>Budget: %{{x}}<br>Average HVAC: %{{y:,.0f}} kWh<extra></extra>",
            )
        )

    fig.update_layout(
        **_layout(
            title=_title("Average HVAC Energy x Insulation Budget"),
            xaxis_title="Budget (wall_r_int + roof_r_int)",
            yaxis_title="Average HVAC energy (kWh/year)",
            legend=_legend(),
        )
    )
    return fig


# -- Figure 3: GSA Morris mu* and sigma -----------------------------------------
def fig_gsa_morris(gsa: dict):
    import plotly.graph_objects as go

    ranking = gsa.get(
        "ranking", sorted(gsa["mu_star"], key=lambda k: -gsa["mu_star"][k])
    )
    mu_vals = [gsa["mu_star"][dp] for dp in ranking]
    sig_vals = [gsa["sigma"][dp] for dp in ranking]
    labels = [DP_LABELS.get(dp, dp) for dp in ranking]

    mu_max = max(mu_vals)
    sig_max = max(sig_vals)
    mu_n = [v / mu_max for v in mu_vals]
    sig_n = [v / sig_max for v in sig_vals]

    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            x=labels,
            y=mu_n,
            name="mu* (importance)",
            marker_color=C["green"],
            opacity=0.85,
            customdata=mu_vals,
            hovertemplate="%{x}<br>mu* = %{customdata:,.0f}<extra></extra>",
        )
    )
    fig.add_trace(
        go.Bar(
            x=labels,
            y=sig_n,
            name="sigma (nonlinear interactions)",
            marker_color=C["blue"],
            opacity=0.65,
            customdata=sig_vals,
            hovertemplate="%{x}<br>sigma = %{customdata:,.0f}<extra></extra>",
        )
    )
    fig.add_hline(
        y=0.05,
        line_dash="dot",
        line_color=C["amber"],
        opacity=0.7,
        annotation_text="threshold 5%",
        annotation_font=dict(color=C["amber"], size=10),
        annotation_position="bottom right",
    )
    fig.add_annotation(
        x=labels[0],
        y=mu_n[0] + 0.04,
        text="Rank #1",
        showarrow=False,
        font=dict(color=C["amber"], size=10),
    )

    n_valid = gsa.get("n_valid", "?")
    fig.update_layout(
        **_layout(
            title=_title(f"GSA -- Morris Method (N=15, {n_valid} valid simulations)"),
            barmode="group",
            yaxis_title="Normalized value (mu*/mu*_max)",
            xaxis_title="Design Parameter",
            legend=_legend(),
        )
    )
    return fig


# -- Figure 4: Normalized trade-off on Pareto front ---------------------------
def fig_pareto_tradeoff(pareto: dict):
    import plotly.graph_objects as go

    pts = [
        p
        for p in pareto.get("pareto_front", [])
        if p.get("hvac_energy_kwh") is not None
    ]
    if not pts:
        return go.Figure()

    energy = [p["hvac_energy_kwh"] for p in pts]
    fr1 = [p["fr1_proxy"] for p in pts]
    e_min, e_max = min(energy), max(energy)
    f_min, f_max = min(fr1), max(fr1)
    e_n = [(e - e_min) / (e_max - e_min + 1e-9) for e in energy]
    f_n = [(f - f_min) / (f_max - f_min + 1e-9) for f in fr1]

    x_vals = list(range(len(pts)))
    x_labels = [f"S{p['setpoint']}C" for p in pts]

    fig = go.Figure()

    fig.add_trace(
        go.Scatter(
            x=x_vals + x_vals[::-1],
            y=e_n + f_n[::-1],
            fill="toself",
            fillcolor="rgba(244,114,182,0.07)",
            line=dict(width=0),
            showlegend=False,
            hoverinfo="skip",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=x_vals,
            y=e_n,
            mode="lines+markers",
            name="FR2 -- Energy (norm.)",
            line=dict(color=C["green"], width=2),
            marker=dict(size=10, symbol="circle"),
            text=x_labels,
            customdata=energy,
            hovertemplate="Solution %{text}<br>Energy: %{customdata:,.0f} kWh<extra></extra>",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=x_vals,
            y=f_n,
            mode="lines+markers",
            name="FR1 -- Comfort (norm.)",
            line=dict(color=C["blue"], width=2),
            marker=dict(size=10, symbol="diamond"),
            text=x_labels,
            customdata=fr1,
            hovertemplate="Solution %{text}<br>FR1 proxy: %{customdata:.3f}<extra></extra>",
        )
    )

    fig.update_layout(
        **_layout(
            title=_title("Trade-off on Pareto Front -- Energy x Comfort"),
            xaxis=dict(
                tickmode="array",
                tickvals=x_vals,
                ticktext=x_labels,
                range=[-0.3, len(pts) - 0.7],
                gridcolor=C["border"],
            ),
            yaxis_title="Normalized value (0 = best, 1 = worst)",
            legend=_legend(),
        )
    )
    return fig


# -- Dash app -----------------------------------------------------------------
def run_dash(pareto: dict, gsa: dict, port: int = 8050):
    try:
        import dash
        from dash import dcc, html
    except ImportError:
        print("Dash nao instalado. Execute: pip install dash")
        print("Exportando HTML estatico como fallback...")
        save_static(pareto, gsa)
        return

    n_sims = len(
        [
            c
            for c in (pareto.get("level2_filtered") or pareto.get("all_candidates", []))
            if c.get("hvac_energy_kwh")
        ]
    )
    n_pareto = len(pareto.get("pareto_front", []))
    n_gsa = gsa.get("n_valid", "?")
    top_dp = (
        DP_LABELS.get(gsa["ranking"][0], gsa["ranking"][0])
        if gsa.get("ranking")
        else "--"
    )
    best_kwh = min(
        (
            p["hvac_energy_kwh"]
            for p in pareto.get("pareto_front", [])
            if p.get("hvac_energy_kwh")
        ),
        default=None,
    )

    def _card(label, value, color):
        return html.Div(
            style={
                "backgroundColor": C["surface"],
                "borderRadius": "6px",
                "padding": "16px 20px",
                "borderLeft": f"3px solid {color}",
                "border": f"1px solid {C['border']}",
            },
            children=[
                html.P(
                    label,
                    style={
                        "color": C["muted"],
                        "fontSize": "10px",
                        "margin": "0 0 4px",
                        "textTransform": "uppercase",
                        "letterSpacing": "0.1em",
                    },
                ),
                html.P(
                    value,
                    style={
                        "color": color,
                        "fontSize": "22px",
                        "fontWeight": "700",
                        "margin": 0,
                    },
                ),
            ],
        )

    def _card_row(graph_component):
        return html.Div(
            style={
                "backgroundColor": C["surface"],
                "border": f"1px solid {C['border']}",
                "borderRadius": "8px",
                "padding": "6px",
            },
            children=[graph_component],
        )

    graph_cfg = {
        "displayModeBar": True,
        "displaylogo": False,
        "modeBarButtonsToRemove": ["lasso2d", "select2d"],
    }

    app = dash.Dash(
        __name__,
        title="EnergyPlus -- Pareto Dashboard",
        suppress_callback_exceptions=True,
    )

    footer_text = (
        f"GSA Morris N=15 · {n_gsa} valid sims · "
        f"Pareto: {n_pareto} solutions · "
        f"Best FR2: {best_kwh:,.0f} kWh"
        if best_kwh
        else f"GSA Morris N=15 · {n_gsa} valid sims"
    )

    app.layout = html.Div(
        style={
            "backgroundColor": C["bg"],
            "minHeight": "100vh",
            "fontFamily": "'IBM Plex Mono', monospace",
            "padding": "24px 32px",
            "color": C["text"],
        },
        children=[
            html.Div(
                style={"marginBottom": "24px"},
                children=[
                    html.H1(
                        "EnergyPlus Optimization Dashboard",
                        style={
                            "fontSize": "20px",
                            "fontWeight": "700",
                            "color": C["green"],
                            "margin": "0 0 4px",
                            "letterSpacing": "0.04em",
                        },
                    ),
                    html.P(
                        "Hierarchical Sequential Multi-Objective Optimization -- USP  "
                        "5ZoneAirCooled  Chicago TMY3  EnergyPlus 25.1.0",
                        style={"color": C["muted"], "fontSize": "11px", "margin": 0},
                    ),
                ],
            ),
            html.Div(
                style={
                    "display": "grid",
                    "gridTemplateColumns": "repeat(4, 1fr)",
                    "gap": "12px",
                    "marginBottom": "20px",
                },
                children=[
                    _card("EnergyPlus Simulations", str(n_sims), C["blue"]),
                    _card("Pareto Solutions", str(n_pareto), C["pink"]),
                    _card("GSA Simulations", str(n_gsa), C["green"]),
                    _card("Most Influential DP", top_dp, C["amber"]),
                ],
            ),
            html.Div(
                style={
                    "display": "grid",
                    "gridTemplateColumns": "3fr 2fr",
                    "gap": "16px",
                    "marginBottom": "16px",
                },
                children=[
                    _card_row(
                        dcc.Graph(
                            figure=fig_pareto_front(pareto),
                            config=graph_cfg,
                            style={"height": "420px"},
                        )
                    ),
                    _card_row(
                        dcc.Graph(
                            figure=fig_energy_by_budget(pareto),
                            config=graph_cfg,
                            style={"height": "420px"},
                        )
                    ),
                ],
            ),
            html.Div(
                style={
                    "display": "grid",
                    "gridTemplateColumns": "1fr 1fr",
                    "gap": "16px",
                },
                children=[
                    _card_row(
                        dcc.Graph(
                            figure=fig_gsa_morris(gsa),
                            config=graph_cfg,
                            style={"height": "380px"},
                        )
                    ),
                    _card_row(
                        dcc.Graph(
                            figure=fig_pareto_tradeoff(pareto),
                            config=graph_cfg,
                            style={"height": "380px"},
                        )
                    ),
                ],
            ),
            html.Div(
                style={
                    "marginTop": "20px",
                    "textAlign": "right",
                    "color": C["muted"],
                    "fontSize": "10px",
                },
                children=[footer_text],
            ),
        ],
    )

    print(f"\nDashboard running at http://localhost:{port}")
    print("Press Ctrl+C to stop.\n")
    app.run(debug=False, port=port, host="0.0.0.0")


# -- Static HTML --------------------------------------------------------------
def save_static(pareto: dict, gsa: dict, output: str = "pareto_dashboard.html"):
    try:
        import plotly.io as pio
        from plotly.subplots import make_subplots
    except ImportError:
        print("plotly not installed. Run: pip install plotly")
        return

    fig = make_subplots(
        rows=2,
        cols=2,
        subplot_titles=[
            "Pareto Front -- FR1 x FR2",
            "Energy x Budget by Orientation",
            "GSA Morris -- mu* and sigma (normalized)",
            "Trade-off on Pareto Front",
        ],
        horizontal_spacing=0.10,
        vertical_spacing=0.14,
    )

    for t in fig_pareto_front(pareto).data:
        fig.add_trace(t, row=1, col=1)
    for t in fig_energy_by_budget(pareto).data:
        fig.add_trace(t, row=1, col=2)
    for t in fig_gsa_morris(gsa).data:
        fig.add_trace(t, row=2, col=1)
    for t in fig_pareto_tradeoff(pareto).data:
        fig.add_trace(t, row=2, col=2)

    axis_style = dict(gridcolor=C["border"], zerolinecolor=C["border"])
    for i in range(1, 5):
        suffix = "" if i == 1 else str(i)
        fig.update_layout(
            **{
                f"xaxis{suffix}": axis_style,
                f"yaxis{suffix}": axis_style,
            }
        )

    # Fix X-axis for Trade-off panel (subplot 4 = xaxis4)
    pts = pareto.get("pareto_front", [])
    if pts:
        fig.update_layout(
            xaxis4=dict(
                tickmode="array",
                tickvals=list(range(len(pts))),
                ticktext=[f"S{p['setpoint']}°C" for p in pts],
                range=[-0.4, len(pts) - 0.6],
                gridcolor=C["border"],
                zerolinecolor=C["border"],
            )
        )

    fig.update_layout(
        paper_bgcolor=C["bg"],
        plot_bgcolor=C["surface"],
        font=dict(family="monospace", color=C["text"], size=11),
        height=900,
        width=1400,
        title=dict(
            text="EnergyPlus Pareto Dashboard -- Hierarchical Sequential Optimization  USP",
            font=dict(size=15, color=C["green"]),
        ),
        showlegend=True,
        legend=dict(bgcolor=C["surface"], bordercolor=C["border"], borderwidth=1),
    )

    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    pio.write_html(fig, str(out), include_plotlyjs="cdn")
    print(f"Dashboard saved to: {out.resolve()}")
    print(f"Open in browser:    file:///{out.resolve()}")


# -- Main -----------------------------------------------------------------------
def main():
    _data = Path(__file__).parent.parent / "data"
    parser = argparse.ArgumentParser(
        description="EnergyPlus Pareto Optimization Dashboard",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--pareto",
        default=str(_data / "pareto_front.json"),
        help="pareto_front.json (default: data/pareto_front.json)",
    )
    parser.add_argument(
        "--gsa",
        default="gsa_results_20260518.json",
        help="gsa_results.json (default: gsa_results_20260518.json)",
    )
    parser.add_argument(
        "--port", type=int, default=8050, help="Dash server port (default: 8050)"
    )
    parser.add_argument(
        "--static",
        action="store_true",
        help="Save static HTML instead of running Dash server",
    )
    parser.add_argument(
        "--output",
        default=str(_data / "pareto_dashboard.html"),
        help="Static HTML file (default: data/pareto_dashboard.html)",
    )
    args = parser.parse_args()

    pareto, gsa = load_data(args.pareto, args.gsa)

    if args.static:
        save_static(pareto, gsa, output=args.output)
    else:
        run_dash(pareto, gsa, port=args.port)


if __name__ == "__main__":
    main()
