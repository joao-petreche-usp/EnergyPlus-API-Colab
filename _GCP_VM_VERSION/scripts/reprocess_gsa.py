"""
reprocess_gsa.py — Reprocesses GSA results from existing eplustbl.htm files
=============================================================================
Usage:
    python _GCP_VM_VERSION/scripts/reprocess_gsa.py [--n-morris N]

Reads eplustbl.htm files already simulated in /tmp/energyplus_sim/gsa/
and recalculates Morris analysis without needing to re-simulate.
"""

import argparse
import json
import re
import sys
from pathlib import Path

import numpy as np

# ── Configuration (must be identical to run_simulation.py) ────────────────────
GSA_DIR = Path("/tmp/energyplus_sim/gsa")
WORK_DIR = Path("/tmp/energyplus_sim")

VARIABLE_NAMES = ["wall_r", "roof_r", "glass_u", "orientation_idx", "setpoint"]
BOUNDS_INT = {
    "wall_r": (5, 25),
    "roof_r": (10, 40),
    "glass_u": (10, 30),
    "orientation_idx": (0, 3),
    "setpoint": (23, 26),
}


# ── Energy extraction from eplustbl.htm ───────────────────────────────────────────
def extract_energy(sim_dir: Path) -> float | None:
    tbl = sim_dir / "eplustbl.htm"
    if not tbl.exists():
        return None
    content = tbl.read_text(encoding="utf-8", errors="ignore")
    match = re.search(
        r"Total Site Energy</td>\s*<td[^>]*>\s*([\d,]+\.?\d*)\s*</td>", content
    )
    if match:
        gj = float(match.group(1).replace(",", ""))
        return round(gj * 277.778, 2)
    return None


# ── Reconstruction of Morris samples ─────────────────────────────────────────────
def reprocess(n_morris: int = 50):
    try:
        from SALib.analyze import morris as morris_analyze
        from SALib.sample import morris as morris_sample
    except ImportError:
        print("SALib not installed. Run: pip install SALib")
        sys.exit(1)

    # SALib problem — identical to run_simulation.py
    problem = {
        "num_vars": 5,
        "names": VARIABLE_NAMES,
        "bounds": [list(BOUNDS_INT[n]) for n in VARIABLE_NAMES],
    }

    # Recreate Morris sampling with same seed
    np.random.seed(42)
    X = morris_sample.sample(problem, N=n_morris, num_levels=4)
    n_samples = len(X)
    print(f"Expected Morris samples: {n_samples}")

    # Collect results from existing simulations
    Y = np.full(n_samples, np.nan)
    missing = []

    for i in range(n_samples):
        sim_dir = GSA_DIR / f"gsa_{i:04d}"
        energy = extract_energy(sim_dir)
        if energy is not None:
            Y[i] = energy
        else:
            missing.append(i)

    valid_mask = ~np.isnan(Y)
    n_valid = valid_mask.sum()
    print(f"Valid simulations    : {n_valid}/{n_samples}")
    print(
        f"Missing simulations  : {len(missing)} — indices: {missing[:10]}{'...' if len(missing) > 10 else ''}"
    )

    if n_valid < 10:
        print("❌ Insufficient simulations for GSA analysis.")
        sys.exit(1)

    # Morris analysis on valid samples
    Si = morris_analyze.analyze(
        problem, X[valid_mask], Y[valid_mask], num_levels=4, print_to_console=False
    )

    # ── Results ─────────────────────────────────────────────────
    print("\n" + "=" * 55)
    print("GSA RESULTS — Morris (µ* and σ)")
    print("=" * 55)
    print(f"  {'DP':<20} {'µ* (importance)':>18} {'σ (interactions)':>16}")
    print("  " + "-" * 54)

    mu_star = Si["mu_star"]
    sigma = Si["sigma"]

    for name, mu, sig in zip(VARIABLE_NAMES, mu_star, sigma):
        print(f"  {name:<20} {mu:>18.4f} {sig:>16.4f}")

    # Ranking by importance
    ranking = sorted(zip(VARIABLE_NAMES, mu_star), key=lambda x: x[1], reverse=True)
    print("\n  Ranking by µ* (suggested lexicographic hierarchy):")
    for rank, (name, mu) in enumerate(ranking, 1):
        print(f"    {rank}. {name:<20} μ* = {mu:.4f}")

    # ── Save results ─────────────────────────────────────────────────────
    results = {
        "n_morris": n_morris,
        "n_samples": n_samples,
        "n_valid": int(n_valid),
        "n_missing": len(missing),
        "missing_indices": missing,
        "mu_star": dict(zip(VARIABLE_NAMES, mu_star.tolist())),
        "sigma": dict(zip(VARIABLE_NAMES, sigma.tolist())),
        "ranking": [name for name, _ in ranking],
        "Y_stats": {
            "min": float(np.nanmin(Y)),
            "max": float(np.nanmax(Y)),
            "mean": float(np.nanmean(Y)),
            "std": float(np.nanstd(Y)),
        },
    }

    out_path = GSA_DIR / "gsa_results.json"
    out_path.write_text(json.dumps(results, indent=2))
    print(f"\n✅ Results saved to: {out_path}")
    print(
        f"\n  Y (energy) — min: {results['Y_stats']['min']:.1f} kWh"
        f"  max: {results['Y_stats']['max']:.1f} kWh"
        f"  average: {results['Y_stats']['mean']:.1f} kWh"
    )
    print("=" * 55)

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Reprocess GSA Morris from existing eplustbl.htm files"
    )
    parser.add_argument(
        "--n-morris",
        type=int,
        default=50,
        help="Number of Morris trajectories (default: 50)",
    )
    args = parser.parse_args()
    if args.n_morris < 1:
        parser.error("--n-morris must be >= 1")
    reprocess(n_morris=args.n_morris)
