"""
gsa_sampler.py — Saltelli sampling for M2 GSA (FR1 + FR2)

Defines the 4-DP problem and generates the Saltelli design matrix.
Used by gsa_runner.py and mode_gsa_sobol in run_simulation.py.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

PROBLEM: dict = {
    "num_vars": 4,
    "names": ["orient_idx", "setpoint", "wall_r", "roof_r"],
    "bounds": [
        [0.0, 3.0],   # orient_idx: snapped to {0,1,2,3}
        [23.0, 26.0], # setpoint: °C (continuous, run_candidate accepts float)
        [0.5, 2.5],   # wall_r: m²K/W
        [1.0, 4.0],   # roof_r: m²K/W
    ],
}


def sample_saltelli(N: int = 128, seed: int = 42) -> np.ndarray:
    """Return Saltelli design matrix of shape (N*(k+2), k) = (768, 4) for N=128, k=4.

    calc_second_order=False keeps N*(k+2) sims instead of N*(2k+2).
    seed is passed to SALib >= 1.4.7; ignored gracefully on older versions.
    """
    from SALib.sample import sobol as saltelli

    try:
        return saltelli.sample(PROBLEM, N, calc_second_order=False, seed=seed)
    except TypeError:
        # Older SALib versions don't accept `seed`; fall back without it
        rng = np.random.default_rng(seed)
        np.random.seed(rng.integers(0, 2**31))
        return saltelli.sample(PROBLEM, N, calc_second_order=False)


def n_sims(N: int) -> int:
    """Return total number of EnergyPlus simulations for a given Saltelli base N."""
    return N * (PROBLEM["num_vars"] + 2)


def save_samples(X: np.ndarray, path: Path) -> None:
    """Save design matrix to CSV with column names from PROBLEM."""
    import pandas as pd

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(X, columns=PROBLEM["names"])
    df.index.name = "sample_idx"
    df.to_csv(path)


def load_samples(path: Path) -> np.ndarray:
    """Load design matrix from CSV saved by save_samples."""
    import pandas as pd

    df = pd.read_csv(path, index_col="sample_idx")
    return df[PROBLEM["names"]].to_numpy()
