"""
gsa_sobol_exact.py - exact Sobol indices via variance decomposition
====================================================================
Computes Sobol first-order (S_i) and total-effect (ST_i) sensitivity
indices DIRECTLY from the M3.7 exhaustive 192-cell full factorial in
exhaustive_pareto.json, with no surrogate and no Saltelli sampling.

Background
----------
The 4 active DPs (orient_idx, setpoint, wall_r, roof_r) are sampled
exhaustively over a discrete grid of size 4 x 4 x 3 x 4 = 192 cells.
For a balanced full factorial, the Sobol variance decomposition is
EXACT (no sampling error):

    V         = Var_X[Y]
    V_k       = Var_{X_k}( E_{X_~k}[Y | X_k] )   -- "main effect" variance
    V_~k      = Var_{X_~k}( E_{X_k}[Y | X_~k] ) -- variance explained by all but X_k
    S_k       = V_k / V                          -- first-order Sobol
    ST_k      = 1 - V_~k / V                     -- total-effect Sobol

These are computed as numpy means/variances on a 4D array indexed by
(orient_idx, setpoint, wall_r, roof_r). No Saltelli sample needed.

Why this is the right answer for paper-1
----------------------------------------
For a balanced full factorial, the ANOVA variance decomposition yields
the exact population-level Sobol indices for the discrete design space
the paper uses — no surrogate, no sampling error, every Y is a real
EnergyPlus output.

Usage
-----
    python3 _GCP_VM_VERSION/scripts/gsa_sobol_exact.py
        --exhaustive  data/exhaustive_pareto.json
        --out         data/sobol_exact.json

Default --exhaustive is /tmp/energyplus_sim/exhaustive/exhaustive_pareto_ref.json
(where run_simulation stages the M3.7 ground truth during a
pareto-sequential run on the VM).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np


_DEFAULT_EXH = Path("/tmp/energyplus_sim/exhaustive/exhaustive_pareto_ref.json")

# Discrete domains the paper enumerates exhaustively
_DP_DOMAINS = {
    "orient_idx": [0, 1, 2, 3],
    "setpoint":   [23, 24, 25, 26],
    "wall_r":     [0.5, 1.5, 2.5],
    "roof_r":     [1.0, 2.0, 3.0, 4.0],
}
_DP_ORDER = ("orient_idx", "setpoint", "wall_r", "roof_r")


def _build_tensor(all_results: list[dict]) -> np.ndarray:
    """Pack the 192 cells into a (4, 4, 3, 4) tensor indexed by DP values."""
    shape = tuple(len(_DP_DOMAINS[k]) for k in _DP_ORDER)
    Y = np.full(shape, np.nan)

    idx_lookup = {
        k: {v: i for i, v in enumerate(_DP_DOMAINS[k])} for k in _DP_ORDER
    }

    n_seen = 0
    for r in all_results:
        if r.get("hvac_energy_kwh") is None:
            continue
        # Setpoint may be stored as float; coerce to int to match domain keys.
        try:
            idx = tuple(
                idx_lookup[k][int(r[k]) if k == "setpoint" else r[k]]
                for k in _DP_ORDER
            )
        except KeyError:
            # Sample outside the canonical grid: skip
            continue
        Y[idx] = r["hvac_energy_kwh"]
        n_seen += 1

    if np.isnan(Y).any():
        missing = int(np.isnan(Y).sum())
        raise SystemExit(
            f"Exhaustive tensor incomplete: {missing} of {Y.size} cells are NaN. "
            "The exhaustive_pareto.json input does not cover the canonical 192-cell grid."
        )

    return Y


def _sobol_exact(Y: np.ndarray) -> dict:
    """Compute exact S_i and ST_i for each of the 4 DPs."""
    V = float(Y.var())
    if V == 0.0:
        raise SystemExit("Total variance is zero. Cannot compute Sobol indices.")

    out: dict = {"total_variance": V}
    indices: dict = {}

    for axis, name in enumerate(_DP_ORDER):
        # Main-effect variance: collapse all OTHER axes by mean, then var on this axis.
        other_axes = tuple(a for a in range(Y.ndim) if a != axis)
        mu_main = Y.mean(axis=other_axes)
        V_k = float(mu_main.var())
        S_k = V_k / V

        # ST_k: collapse ONLY this axis by mean, then var over the remaining axes.
        # 1 - Var(E[Y | X_~k]) / V = fraction of variance involving X_k somehow.
        mu_complement = Y.mean(axis=axis)
        V_complement = float(mu_complement.var())
        ST_k = 1.0 - V_complement / V

        indices[name] = {
            "S1": S_k,
            "ST": ST_k,
            "interaction": ST_k - S_k,  # captures non-additive contributions
        }

    out["indices"] = indices
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Exact Sobol indices via full-factorial variance decomposition.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--exhaustive",
        type=Path,
        default=_DEFAULT_EXH,
        help=f"Path to exhaustive_pareto.json (default: {_DEFAULT_EXH}).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("/tmp/energyplus_sim/gsa_sobol_exact/sobol_exact.json"),
        help="Output JSON path.",
    )
    args = parser.parse_args()

    if not args.exhaustive.exists():
        raise SystemExit(f"Not found: {args.exhaustive}")

    data = json.loads(args.exhaustive.read_text(encoding="utf-8"))
    all_results = data.get("all_results")
    if not all_results:
        raise SystemExit(
            f"{args.exhaustive} has no 'all_results' list - not a valid exhaustive output."
        )

    print(f"Loaded {len(all_results)} cells from {args.exhaustive}")
    Y = _build_tensor(all_results)
    print(f"Tensor shape {Y.shape}, mean {Y.mean():.1f} kWh, std {Y.std():.1f} kWh")

    result = _sobol_exact(Y)

    # Ranking by ST (descending)
    ranking = sorted(result["indices"].items(), key=lambda kv: kv[1]["ST"], reverse=True)
    result["ranking_by_ST"] = [name for name, _ in ranking]

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    # Human-readable summary
    print("\n=== Exact Sobol indices (variance decomposition over the 192-cell full factorial) ===")
    print(f"{'DP':<14} {'S1':>10} {'ST':>10} {'ST-S1':>10}")
    print("-" * 50)
    for name in result["ranking_by_ST"]:
        vals = result["indices"][name]
        print(f"{name:<14} {vals['S1']:>10.4f} {vals['ST']:>10.4f} {vals['interaction']:>10.4f}")
    print(f"\nTotal variance: {result['total_variance']:.1f} kWh^2")
    print(f"\nWrote: {args.out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
