"""
gsa_runner.py — Batch EnergyPlus runner for M2 GSA (FR1 + FR2)

Translates a Saltelli design matrix into run_batch tasks with return_comfort=True,
executes them, and assembles the results into a DataFrame with both responses.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import numpy as np
import pandas as pd
from src.config import OCCUPIED_ZONES

log = logging.getLogger(__name__)

# orient_idx is categorical — snap continuous Saltelli sample to nearest integer
_ORIENT_VALUES = (0, 1, 2, 3)


def _snap_orient(x: float) -> int:
    return max(0, min(3, int(round(x))))


def build_tasks(
    X: np.ndarray,
    idf_path: Path,
    epw_path: Path,
    work_dir: Path,
    quiet: bool = True,
    comfort_threshold: "float | None" = None,
) -> list[dict]:
    """Translate Saltelli design matrix rows into run_batch task dicts.

    Each task includes return_comfort=True so run_candidate returns
    {"hvac_kwh": float, "discomfort_kh": float} instead of a bare float.

    comfort_threshold: fixed °C threshold for FR1 (e.g. 24.0). If None,
    uses the dynamic setpoint (always 0 when HVAC is active).

    Column order must match gsa_sampler.PROBLEM["names"]:
        [orient_idx, setpoint, wall_r, roof_r]
    """
    import run_simulation as rs  # noqa: F401

    orient_map = rs.ORIENTATION_MAP
    tasks = []
    for i, row in enumerate(X):
        orient = _snap_orient(float(row[0]))
        setpoint = float(row[1])
        wall_r = float(row[2])
        roof_r = float(row[3])

        key = f"gsa_n{i:05d}_o{orient}_sp{setpoint:.2f}_w{wall_r:.3f}_r{roof_r:.3f}"
        tasks.append(
            {
                "key": key,
                "idf_base": idf_path,
                "epw_path": epw_path,
                "output_dir": work_dir,
                "wall_r": wall_r,
                "roof_r": roof_r,
                "orientation": orient_map[orient],
                "setpoint": setpoint,
                "candidate_id": key,
                "quiet": quiet,
                "return_comfort": True,
                "return_zonal_comfort": True,
                "comfort_threshold": comfort_threshold,
                # store snapped sample for DataFrame assembly
                "_sample_row": [orient, setpoint, wall_r, roof_r],
            }
        )
    return tasks


def run_and_collect(
    tasks: list[dict],
    workers: int,
    chunk_size: int = 256,
    keep_run_dirs: bool = False,
) -> pd.DataFrame:
    """Run all tasks in chunks and return a DataFrame with columns:

        orient_idx, setpoint, wall_r, roof_r, fr1_kh, fr2_kwh[, fr1_<zone>_kh ...]

    fr1_kh  = annual discomfort degree-hours (total across all zones)
    fr2_kwh = annual HVAC electricity (kWh)
    fr1_<zone>_kh = per-zone discomfort (added when return_zonal_comfort=True)
    Rows with failed sims have NaN for all FR columns.
    """
    import run_simulation as rs

    work_dir = tasks[0]["output_dir"] if tasks else Path("/tmp")
    n = len(tasks)
    n_chunks = max(1, (n + chunk_size - 1) // chunk_size)
    results_by_key: dict = {}

    for ci in range(n_chunks):
        chunk = tasks[ci * chunk_size : (ci + 1) * chunk_size]
        log.info(f"chunk {ci + 1}/{n_chunks}: {len(chunk)} sims")
        batch = rs.run_batch(chunk, workers, label=f"gsa-c{ci+1}", log_every=50)
        results_by_key.update(batch)

        if not keep_run_dirs:
            for t in chunk:
                d = work_dir / t["key"]
                if d.exists():
                    shutil.rmtree(d, ignore_errors=True)

    # Assemble DataFrame in task order (required by SALib)
    n_zones = len(OCCUPIED_ZONES)
    rows = []
    n_failed = 0
    for t in tasks:
        sample = t["_sample_row"]
        result = results_by_key.get(t["key"])
        if result is None or not isinstance(result, dict):
            n_failed += 1
            rows.append(sample + [float("nan"), float("nan")] + [float("nan")] * n_zones)
        else:
            discomfort = result.get("discomfort_kh", float("nan"))
            if isinstance(discomfort, dict):
                total_fr1 = sum(discomfort.values())
                zone_vals = [discomfort.get(z, float("nan")) for z in OCCUPIED_ZONES]
            else:
                total_fr1 = discomfort
                zone_vals = [float("nan")] * n_zones
            rows.append(sample + [total_fr1, result.get("hvac_kwh", float("nan"))] + zone_vals)

    if n_failed:
        log.warning(f"{n_failed}/{n} sims failed — those rows will have NaN FRs.")

    zone_cols = [f"fr1_{z}_kh" for z in OCCUPIED_ZONES]
    return pd.DataFrame(
        rows,
        columns=["orient_idx", "setpoint", "wall_r", "roof_r", "fr1_kh", "fr2_kwh"] + zone_cols,
    )


def save_results(df: pd.DataFrame, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.index.name = "sample_idx"
    df.to_csv(path)
    log.info(f"Saved GSA results → {path}  ({len(df)} rows)")


def load_results(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, index_col="sample_idx")
