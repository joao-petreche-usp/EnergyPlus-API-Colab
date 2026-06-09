"""
gsa_analysis.py — Sobol index computation for FR1 and FR2 (M2.5)

Wraps SALib sobol.analyze for both functional responses and serialises
the combined result to data/gsa_results.json.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)


def compute_sobol(Y: np.ndarray, problem: dict) -> dict:
    """Return S1/ST with 95% bootstrap CI for a single Y vector.

    Aborts if Y contains NaN — mean imputation biases Sobol indices.
    """
    from SALib.analyze import sobol as sobol_analyze

    if np.any(np.isnan(Y)):
        n_nan = int(np.sum(np.isnan(Y)))
        raise ValueError(
            f"Y contains {n_nan} NaN values. Fix failed sims before analysis."
        )

    Si = sobol_analyze.analyze(
        problem, Y, calc_second_order=False, conf_level=0.95, print_to_console=False
    )
    names = problem["names"]
    return {
        "S1":      dict(zip(names, Si["S1"].tolist())),
        "ST":      dict(zip(names, Si["ST"].tolist())),
        "S1_conf": dict(zip(names, Si["S1_conf"].tolist())),
        "ST_conf": dict(zip(names, Si["ST_conf"].tolist())),
    }


def analyze_both(
    Y_fr1: np.ndarray,
    Y_fr2: np.ndarray,
    problem: dict,
    n_samples: int,
    out_path: Path | None = None,
) -> dict:
    """Compute Sobol indices for FR1 and FR2, log a summary, optionally save JSON.

    Returns:
        {
          "fr1": {S1, ST, S1_conf, ST_conf},
          "fr2": {S1, ST, S1_conf, ST_conf},
          "n_samples": N,
          "n_sims": len(Y_fr1),
          "problem": problem,
        }
    """
    log.info("Computing Sobol indices for FR1 (discomfort degree-hours)…")
    fr1_indices = compute_sobol(Y_fr1, problem)

    log.info("Computing Sobol indices for FR2 (HVAC energy kWh)…")
    fr2_indices = compute_sobol(Y_fr2, problem)

    result = {
        "fr1": fr1_indices,
        "fr2": fr2_indices,
        "n_samples": n_samples,
        "n_sims": int(len(Y_fr1)),
        "problem": problem,
    }

    _log_summary(result)

    if out_path is not None:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        log.info(f"Saved Sobol results → {out_path}")

    return result


def _log_summary(result: dict) -> None:
    names = result["problem"]["names"]
    header = f"{'DP':<14} {'FR1 S1':>8} {'±95%':>8}  {'FR2 S1':>8} {'±95%':>8}  {'FR1 ST':>8}  {'FR2 ST':>8}"
    log.info("\n=== Sobol Summary (S1 / ST) ===")
    log.info(header)
    log.info("-" * len(header))
    for name in names:
        fr1 = result["fr1"]
        fr2 = result["fr2"]
        log.info(
            f"{name:<14} "
            f"{fr1['S1'][name]:>8.4f} {fr1['S1_conf'][name]:>8.4f}  "
            f"{fr2['S1'][name]:>8.4f} {fr2['S1_conf'][name]:>8.4f}  "
            f"{fr1['ST'][name]:>8.4f}  {fr2['ST'][name]:>8.4f}"
        )


def load_results(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
