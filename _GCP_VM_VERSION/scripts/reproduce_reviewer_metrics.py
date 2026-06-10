"""
reproduce_reviewer_metrics.py — Orchestrator for Reviewer-2 hardening artifacts.

Runs the FR1 proxy calibration and the discretisation-bias study in sequence,
then prints a one-screen summary. Used by peer reviewers (and by Section 5.2
of the manuscript) to regenerate the empirical evidence from a clean clone.

Inputs: _GCP_VM_VERSION/data/gsa_results_v2.csv (cached 768-sample GSA dataset).
Outputs:
    _GCP_VM_VERSION/data/proxy_calibration_p1.json
    _GCP_VM_VERSION/data/discretization_sensitivity.json
    figures/fig_proxy_calibration.{pdf,png}
    figures/fig_discretization_bar.{pdf,png}

Runtime: ~10-30 seconds total on a laptop (no EnergyPlus runs, polynomial-
surrogate-only). See plan/manuscript/REVIEWER2_HARDENING_PLAN.md for context.

Usage (from repo root):
    python -u _GCP_VM_VERSION/scripts/reproduce_reviewer_metrics.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GCP = ROOT / "_GCP_VM_VERSION"
SCRIPTS = GCP / "scripts"
DATA = GCP / "data"


def _run(script: Path) -> int:
    print(f"\n=== Running {script.relative_to(ROOT)} ===")
    result = subprocess.run(
        [sys.executable, "-u", str(script)],
        cwd=str(ROOT),
    )
    return result.returncode


def main() -> int:
    rc1 = _run(SCRIPTS / "calibrate_fr1_proxy.py")
    rc2 = _run(SCRIPTS / "sobol_continuous_vs_discrete.py")

    prox = json.loads((DATA / "proxy_calibration_p1.json").read_text())
    disc = json.loads((DATA / "discretization_sensitivity.json").read_text())

    print("\n" + "=" * 70)
    print(" Reviewer-2 hardening — empirical summary")
    print("=" * 70)
    print(f"\n  [1] FR1 proxy calibration (Reviewer Concern 1)")
    print(f"      R^2                = {prox['r2']:.4f}")
    print(f"      MAE                = {prox['mae_kh']:.1f} kh")
    print(f"      Spearman rho       = {prox['spearman_rho']:+.4f}")
    print(f"      Kendall tau        = {prox['kendall_tau']:+.4f}")
    print(f"      Sign matches docs  = {prox['sign_matches_proxy_doc']}")
    print(f"      Overall pass       = {prox['overall_pass']}")
    print()
    print(f"  [2] Discretisation bias (Reviewer Concern 3)")
    print(f"      max |Delta S_T| (S_T > {disc['inert_floor']:.2f}) = {disc['max_deviation_pct_nontrivial']:.2f}%")
    print(f"      max |abs Delta S_T|         = {disc['max_abs_delta_st']:.4f}")
    print(f"      Ranking preserved           = {disc['ranking_preserved']}")
    print(f"      Overall pass                = {disc['overall_pass']}")
    print()
    print(f"Outputs:")
    print(f"  {(DATA / 'proxy_calibration_p1.json').relative_to(ROOT)}")
    print(f"  {(DATA / 'discretization_sensitivity.json').relative_to(ROOT)}")
    print(f"  figures/fig_proxy_calibration.{{pdf,png}}")
    print(f"  figures/fig_discretization_bar.{{pdf,png}}")
    print()
    print("See plan/manuscript/manuscript.md Section 5.2 for narrative context.")
    print("=" * 70)

    return 0 if (rc1 == 0 and rc2 == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
