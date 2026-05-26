"""
build_proxies.py — Fit the two CP-SAT linearization proxies (M3.1 / M3.2)
==========================================================================
Reads a calibration dataset (`run_simulation.py --mode calibrate`) and fits the
two linearization variants the CP-SAT `mode_pareto` will use (M3.3):

  Variant A (regression):  energy ≈ Σ aᵢ·xᵢ + b   — numpy least-squares;
                           signed coefficients derived automatically.
  Variant B (marginal):    energy ≈ Σ marginal[i][xᵢ] — grouped means per DP
                           value; additive lookup, no sign reasoning.

Writes:
  data/proxy_coefficients.json   (Variant A)
  data/marginal_tables.json      (Variant B)
both with R² / MAE fit quality (in-sample + held-out).

Usage:
    python _GCP_VM_VERSION/scripts/build_proxies.py
    python _GCP_VM_VERSION/scripts/build_proxies.py --samples /path/to/calibration_samples.json
"""

import argparse
import json
from pathlib import Path

import numpy as np

DP_ORDER = ["orient_idx", "setpoint", "wall_r", "roof_r"]
SCALE = 100  # integer scaling for CP-SAT integer arithmetic


def load_samples(path: Path) -> tuple[dict, list[dict]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = [r for r in data["samples"] if r.get("hvac_energy_kwh") is not None]
    if not rows:
        raise SystemExit(f"No valid samples in {path}")
    return data, rows


def _design_matrix(rows: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    X = np.array([[r[dp] for dp in DP_ORDER] for r in rows], dtype=float)
    y = np.array([r["hvac_energy_kwh"] for r in rows], dtype=float)
    return X, y


# ── Variant A: linear regression ──────────────────────────────────────────────
def fit_regression(rows: list[dict]) -> dict:
    X, y = _design_matrix(rows)
    A = np.hstack([X, np.ones((len(X), 1))])
    sol, *_ = np.linalg.lstsq(A, y, rcond=None)
    pred = A @ sol
    ss_res = float(np.sum((y - pred) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    mae = float(np.mean(np.abs(y - pred)))

    # Held-out MAE (80/20 split)
    rng = np.random.default_rng(42)
    idx = rng.permutation(len(X))
    n_test = max(1, int(len(X) * 0.2))
    te, tr = idx[:n_test], idx[n_test:]
    A_tr = np.hstack([X[tr], np.ones((len(tr), 1))])
    sol_tr, *_ = np.linalg.lstsq(A_tr, y[tr], rcond=None)
    A_te = np.hstack([X[te], np.ones((len(te), 1))])
    heldout_mae = float(np.mean(np.abs(y[te] - A_te @ sol_tr)))

    coefs = {dp: float(sol[i]) for i, dp in enumerate(DP_ORDER)}
    return {
        "dp_order": DP_ORDER,
        "coefficients_kwh": coefs,
        "intercept_kwh": float(sol[-1]),
        "coefficients_int": {dp: int(round(v * SCALE)) for dp, v in coefs.items()},
        "scale": SCALE,
        "fit": {"r2": round(r2, 4), "mae_kwh": round(mae, 2),
                "heldout_mae_kwh": round(heldout_mae, 2), "n_samples": len(rows)},
    }


# ── Variant B: marginal contribution tables ──────────────────────────────────
def fit_marginal(data: dict, rows: list[dict]) -> dict:
    values = {
        "orient_idx": [0, 1, 2, 3],
        "setpoint": [23, 24, 25, 26],
        "wall_r": data["wall_r_grid"],
        "roof_r": data["roof_r_grid"],
    }
    grand = float(np.mean([r["hvac_energy_kwh"] for r in rows]))

    table_kwh, table_int, missing = {}, {}, []
    for dp in DP_ORDER:
        col = []
        for v in values[dp]:
            ev = [r["hvac_energy_kwh"] for r in rows if r[dp] == v]
            if ev:
                col.append(float(np.mean(ev)))
            else:
                col.append(grand)  # no sample at this value — neutral fill
                missing.append(f"{dp}={v}")
        table_kwh[dp] = col
        table_int[dp] = [int(round(v * SCALE)) for v in col]

    # Additive-model MAE: energy ≈ Σ marginal[i][xᵢ] − (K−1)·grand_mean
    k = len(DP_ORDER)
    errs = []
    for r in rows:
        pred = sum(table_kwh[dp][values[dp].index(r[dp])] for dp in DP_ORDER) - (k - 1) * grand
        errs.append(abs(r["hvac_energy_kwh"] - pred))

    return {
        "dp_order": DP_ORDER,
        "values": values,
        "table_kwh": table_kwh,
        "table_int": table_int,
        "scale": SCALE,
        "grand_mean_kwh": round(grand, 2),
        "fit": {"additive_mae_kwh": round(float(np.mean(errs)), 2),
                "n_samples": len(rows), "missing_cells": missing},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Fit CP-SAT linearization proxies")
    parser.add_argument(
        "--samples",
        default="/tmp/energyplus_sim/calibrate/calibration_samples.json",
        help="Path to calibration_samples.json (from --mode calibrate)",
    )
    args = parser.parse_args()

    data_dir = Path(__file__).resolve().parent.parent / "data"
    data, rows = load_samples(Path(args.samples))
    print(f"Loaded {len(rows)} valid calibration samples from {args.samples}")

    variant_a = fit_regression(rows)
    variant_b = fit_marginal(data, rows)

    (data_dir / "proxy_coefficients.json").write_text(
        json.dumps(variant_a, indent=2), encoding="utf-8"
    )
    (data_dir / "marginal_tables.json").write_text(
        json.dumps(variant_b, indent=2), encoding="utf-8"
    )

    fa = variant_a["fit"]
    print("\nVariant A — linear regression")
    print(f"  R² = {fa['r2']}  |  MAE = {fa['mae_kwh']} kWh  |  held-out MAE = {fa['heldout_mae_kwh']} kWh")
    for dp, c in variant_a["coefficients_kwh"].items():
        print(f"    {dp:12s} {c:+10.2f} kWh/unit")
    print(f"  → data/proxy_coefficients.json")

    fb = variant_b["fit"]
    print("\nVariant B — marginal tables")
    print(f"  additive-model MAE = {fb['additive_mae_kwh']} kWh")
    if fb["missing_cells"]:
        print(f"  [!] cells with no sample (filled with grand mean): {fb['missing_cells']}")
    print(f"  → data/marginal_tables.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
