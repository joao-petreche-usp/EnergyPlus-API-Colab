"""
run_simulation.py — EnergyPlus GCP VM Standalone Runner
========================================================
Replaces the EnergyPlus_API_GCP_VM.ipynb notebook for CLI execution.

Execution modes:
    validate   — 1 simulation with base parameters, analyzes eplusout.err
    single     — 1 simulation with DPs specified via argparse
    gsa        — Global Sensitivity Analysis (Morris) on the 5 real DPs
    pareto     — Complete Pipeline: GSA → triangularization → Lexicographic Pareto
    exhaustive — Grid search over 192 combinations for empirical Pareto ground truth (M3.7)
    calibrate  — LHS sampling run to calibrate the two CP-SAT linearization proxies (M3.1)

Usage:
    # Quick validation (default mode)
    python run_simulation.py --mode validate

    # Single simulation with custom parameters
    python run_simulation.py --mode single --setpoint 24 --orientation 0 --wall-r 2.0 --roof-r 3.5

    # GSA with 100 Morris trajectories
    python run_simulation.py --mode gsa --n-morris 100

    # Complete pipeline
    python run_simulation.py --mode pareto

    # Exhaustive baseline (192 sims, ~1.6h)
    python run_simulation.py --mode exhaustive

VM Authentication:
    Workload Identity via Service Account attached to the instance.
    No gcloud auth required inside the VM.

Stage-In/Stage-Out:
    Inputs  : gs://eplus-colab-cloud-data/models/ and weather/
    Outputs : gs://eplus-colab-cloud-data/results/gcp_vm_{mode}_{timestamp}/
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# Add _GCP_VM_VERSION/ to PYTHONPATH early for src/ imports
_gcp_vm_dir = Path(__file__).resolve().parent
if str(_gcp_vm_dir) not in sys.path:
    sys.path.insert(0, str(_gcp_vm_dir))

import numpy as np
from src.config import (
    ACTUATOR_COMPONENT_TYPE,
    ACTUATOR_CONTROL_TYPE,
    BUILDING_OBJECT_NAME,
    CLG_SCHEDULE_NAME,
    DP_DOMAINS,
    EPLUS_DIR,
    INSULATION_MODE,
    K_IN02,
    K_IN46,
    OCCUPIED_ZONES,
    ORIENTATION_MAP,
    ROOF_INSULATION_NAME,
    WALL_INSULATION_NAME,
)

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Project Constants ─────────────────────────────────────────────────────────
DEFAULT_BUCKET = "eplus-colab-cloud-data"
DEFAULT_IDF_GCS = "models/5ZoneAirCooled.idf"
DEFAULT_EPW_GCS = "weather/USA_IL_Chicago-OHare.Intl.AP.725300_TMY3.epw"
DEFAULT_BUFFALO_EPW_GCS = "weather/USA_NY_Buffalo-Greater.Buffalo.Intl.AP.725280_TMY3.epw"
WORK_DIR = Path("/tmp/energyplus_sim")

# glass_u held constant until glazing injection lands (M1.6); see mode_exhaustive.
GLASS_U_FIXED = 2.0


# GSA Hierarchy — Morris N=15, 90 valid simulations (2026-05-18)
GSA_HIERARCHY: dict[str, dict] = {
    "orientation_idx": {"rank": 1, "mu_star": 4548.9},
    "wall_r": {"rank": 2, "mu_star": 2646.1},
    "roof_r": {"rank": 3, "mu_star": 2548.6},
    "setpoint": {"rank": 4, "mu_star": 2274.2},
    "glass_u": {"rank": 5, "mu_star": 1906.4},
}


# ── GCS helpers ──────────────────────────────────────────────────────────────
def _gcs_available() -> bool:
    try:
        from google.cloud import storage  # noqa: F401

        return True
    except ImportError:
        return False


def stage_in(bucket: str, gcs_path: str, local_path: Path) -> bool:
    """Download file from GCS to local. Returns True if successful."""
    local_path.parent.mkdir(parents=True, exist_ok=True)
    if local_path.exists():
        log.info(f"  Local cache: {local_path.name}")
        return True
    try:
        from google.cloud import storage

        client = storage.Client()
        blob = client.bucket(bucket).blob(gcs_path)
        blob.download_to_filename(str(local_path))
        log.info(f"  ✅ Stage-In: gs://{bucket}/{gcs_path} → {local_path}")
        return True
    except Exception as e:
        log.error(f"  ❌ Stage-In failed ({gcs_path}): {e}")
        return False


def stage_out(bucket: str, local_dir: Path, gcs_prefix: str) -> int:
    """Upload all files from local_dir to GCS. Returns number of files sent."""
    if not _gcs_available():
        log.warning("  google-cloud-storage not installed — Stage-Out skipped.")
        return 0
    try:
        from google.cloud import storage

        client = storage.Client()
        bkt = client.bucket(bucket)
        count = 0
        for f in sorted(local_dir.iterdir()):
            if f.is_file():
                blob = bkt.blob(f"{gcs_prefix}/{f.name}")
                blob.upload_from_filename(str(f))
                count += 1
        log.info(f"  ✅ Stage-Out: {count} files → gs://{bucket}/{gcs_prefix}/")
        return count
    except Exception as e:
        log.error(f"  ❌ Stage-Out failed: {e}")
        return 0


# ── EnergyPlus helpers ────────────────────────────────────────────────────────
def _load_eplus_api():
    """Import EnergyPlusAPI, adding EPLUS_DIR to sys.path if needed."""
    if EPLUS_DIR not in sys.path:
        sys.path.insert(0, EPLUS_DIR)
    try:
        from pyenergyplus.api import EnergyPlusAPI

        return EnergyPlusAPI()
    except ImportError as e:
        log.error(f"pyenergyplus not found at {EPLUS_DIR}: {e}")
        sys.exit(1)


def _extract_hvac_energy(output_dir: Path) -> Optional[float]:
    """
    Extract total site energy (kWh) from eplustbl.htm.
    HTML pattern: numeric value in <td> immediately after 'Total Site Energy'.
    232.27 GJ × 277.778 = 64,519 kWh
    """
    tbl = output_dir / "eplustbl.htm"
    if not tbl.exists():
        log.warning(f"  eplustbl.htm not found at {output_dir}")
        return None
    try:
        content = tbl.read_text(encoding="utf-8", errors="ignore")
        match = re.search(
            r"Total Site Energy</td>\s*<td[^>]*>\s*([\d,]+\.?\d*)\s*</td>", content
        )
        if match:
            gj = float(match.group(1).replace(",", ""))
            return round(gj * 277.778, 2)
        log.warning("  Could not extract energy from eplustbl.htm")
        return None
    except Exception as e:
        log.error(f"  Error parsing eplustbl.htm: {e}")
        return None


def _check_err(output_dir: Path) -> tuple[int, int]:
    """Count warnings and errors in eplusout.err. Returns (warnings, errors)."""
    err_file = output_dir / "eplusout.err"
    if not err_file.exists():
        return 0, 0
    content = err_file.read_text(encoding="utf-8", errors="ignore")
    warnings = len(re.findall(r"^\s*\*\* Warning \*\*", content, re.MULTILINE))
    errors = len(re.findall(r"^\s*\*\* (Severe|Fatal) \*\*", content, re.MULTILINE))
    return warnings, errors


@contextlib.contextmanager
def _eplus_quiet():
    """Suprime output C-level do EnergyPlus (Warming up, Initializing...) via os.dup2."""
    devnull_fd = os.open(os.devnull, os.O_WRONLY)
    saved_stdout = os.dup(1)
    saved_stderr = os.dup(2)
    try:
        os.dup2(devnull_fd, 1)
        os.dup2(devnull_fd, 2)
        yield
    finally:
        os.dup2(saved_stdout, 1)
        os.dup2(saved_stderr, 2)
        os.close(devnull_fd)
        os.close(saved_stdout)
        os.close(saved_stderr)


# ── Parallel batch infrastructure ─────────────────────────────────────────────
# EnergyPlus has process-global C/C++ state, so batch parallelism uses separate
# worker PROCESSES (not threads). Each worker builds its own EnergyPlus API once.
_WORKER_API = None


def _worker_init():
    """Pool initializer — build this worker process's own EnergyPlus API once."""
    global _WORKER_API
    _WORKER_API = _load_eplus_api()


def _run_candidate_worker(task: dict):
    """Picklable pool task: run ONE simulation, never raise.

    `task` is a plain dict (see run_batch). Returns (key, energy_or_None); `key`
    lets the caller map the result back to its candidate.
    """
    global _WORKER_API
    if _WORKER_API is None:  # lazy fallback (sequential path / --workers 1)
        _WORKER_API = _load_eplus_api()
    try:
        energy = run_candidate(
            _WORKER_API,
            task["idf_base"],
            task["epw_path"],
            task["output_dir"],
            wall_r=task["wall_r"],
            roof_r=task["roof_r"],
            orientation=task["orientation"],
            setpoint=task["setpoint"],
            candidate_id=task["candidate_id"],
            quiet=task["quiet"],
            return_comfort=task.get("return_comfort", False),
            return_zonal_comfort=task.get("return_zonal_comfort", False),
            comfort_threshold=task.get("comfort_threshold", None),
        )
    except Exception:  # a crashed sim must not abort the whole pool
        log.warning(
            f"  Simulation {task['candidate_id']} raised an exception:",
            exc_info=True,
        )
        energy = None
    return task["key"], energy


def run_batch(
    tasks: list[dict],
    workers: int,
    *,
    label: str = "batch",
    log_every: int = 10,
) -> dict:
    """Run a list of EnergyPlus candidate tasks, sequentially or in a process pool.

    Each task dict carries the keys consumed by _run_candidate_worker, including a
    unique hashable `key` used to map results back to the caller.

    workers <= 1 → sequential, in-process (the original behaviour; A/B baseline).
    workers >= 2 → multiprocessing.Pool, one EnergyPlus API per worker process.

    Returns {key: energy_or_None} for every task. Order-independent.
    """
    import multiprocessing as mp
    import time

    n = len(tasks)
    results: dict = {}
    start = time.time()
    log.info(f"  {label}: {n} simulations on {max(1, workers)} worker(s)")

    def _log_progress(done: int) -> None:
        if done % log_every == 0 or done == n:
            elapsed = time.time() - start
            eta = (elapsed / done) * (n - done) if done else 0.0
            valid = sum(v is not None for v in results.values())
            log.info(
                f"  Progress: {done}/{n} | Valid: {valid} | ETA: {eta / 60:.1f} min"
            )

    if workers <= 1:
        for i, task in enumerate(tasks):
            key, energy = _run_candidate_worker(task)
            results[key] = energy
            _log_progress(i + 1)
        return results

    ctx = mp.get_context("spawn")
    with ctx.Pool(processes=workers, initializer=_worker_init) as pool:
        for i, (key, energy) in enumerate(
            pool.imap_unordered(_run_candidate_worker, tasks)
        ):
            results[key] = energy
            _log_progress(i + 1)
    return results


# ── Core evaluation function (used by GSA and single mode) ───────────────────
def run_candidate(
    api,
    idf_base: Path,
    epw_path: Path,
    output_dir: Path,
    wall_r: float,  # R-value wall insulation (e.g. 2.5)
    roof_r: float,  # R-value roof insulation (e.g. 3.5)
    orientation: float,  # North angle in degrees (0, 90, 180, 270)
    setpoint: "float | dict[str, float]",  # °C scalar (legacy) or dict per-zone
    candidate_id: str = "candidate",
    quiet: bool = False,
    return_comfort: bool = False,
    comfort_threshold: "float | None" = None,
    return_zonal_comfort: bool = False,
) -> "Optional[float] | Optional[dict]":
    """Run 1 EnergyPlus simulation with the provided DPs.

    setpoint: if float, applied globally to CLG_SCHEDULE_NAME.
              if dict, applied per-zone via ZONE_SCHEDULE_NAMES.
    return_comfort=False: returns annual HVAC energy (kWh) or None.
    return_comfort=True: returns {"hvac_kwh": float, "discomfort_kh": float}
    return_zonal_comfort=True: returns {"hvac_kwh": float, "discomfort_kh": dict[str, float]}
    """
    from src.config import ZONE_SCHEDULE_NAMES  # noqa: PLC0415
    from src.utils.idf_patcher import IDFPatcher

    sim_dir = output_dir / candidate_id
    sim_dir.mkdir(parents=True, exist_ok=True)
    temp_idf = sim_dir / f"model_{candidate_id}.idf"

    # 1. Pre-processing: patch IDF (geometry + envelope)
    patcher = IDFPatcher(idf_base)
    patcher.reset()
    patcher.set_north_axis(orientation, BUILDING_OBJECT_NAME)
    if INSULATION_MODE == "nomass":
        patcher.set_nomass_resistance(WALL_INSULATION_NAME, wall_r)
        patcher.set_nomass_resistance(ROOF_INSULATION_NAME, roof_r)
    else:
        patcher.set_material_thickness(WALL_INSULATION_NAME, wall_r * K_IN02)
        patcher.set_material_thickness(ROOF_INSULATION_NAME, roof_r * K_IN46)
    patcher.save(temp_idf)

    # 2. Runtime: setpoint injection + optional FR1 comfort tracking via Exchange API
    _initialized = False
    _actuators: dict[str, int] = {}  # schedule_name -> handle
    _zone_handles: dict[str, int] = {}  # zone_name -> handle
    
    # Comfort accumulators
    _global_comfort_kh = 0.0
    _zonal_comfort_kh = {z: 0.0 for z in OCCUPIED_ZONES}

    # Resolve threshold logic
    _thresh_global = comfort_threshold if comfort_threshold is not None else (setpoint if isinstance(setpoint, (int, float)) else 24.0)
    _thresh_zonal = {}
    for z in OCCUPIED_ZONES:
        if comfort_threshold is not None:
            _thresh_zonal[z] = comfort_threshold
        elif isinstance(setpoint, dict):
            _thresh_zonal[z] = setpoint.get(z, 24.0)
        else:
            _thresh_zonal[z] = setpoint

    def _on_begin_zone_timestep(state_arg):
        nonlocal _initialized, _actuators, _zone_handles
        if not _initialized and api.exchange.api_data_fully_ready(state_arg):
            # Request Actuators
            if isinstance(setpoint, dict):
                # Multi-zone mode: get per-zone handles
                for z, actuator_key in ZONE_SCHEDULE_NAMES.items():
                    h = api.exchange.get_actuator_handle(
                        state_arg, ACTUATOR_COMPONENT_TYPE, ACTUATOR_CONTROL_TYPE, actuator_key
                    )
                    if h != -1:
                        _actuators[actuator_key] = h
                    else:
                        log.warning(f"  Actuator handle not found: {actuator_key}")
            else:
                # Legacy mode: get single global handle
                h = api.exchange.get_actuator_handle(
                    state_arg, "Schedule:Compact", "Schedule Value", CLG_SCHEDULE_NAME
                )
                if h != -1:
                    _actuators[CLG_SCHEDULE_NAME] = h

            # Request Comfort Variables
            if return_comfort or return_zonal_comfort:
                for z in OCCUPIED_ZONES:
                    h = api.exchange.get_variable_handle(
                        state_arg, "Zone Mean Air Temperature", z
                    )
                    if h != -1:
                        _zone_handles[z] = h
                    else:
                        log.warning(f"  Zone temperature handle not found: {z!r}")
            _initialized = True

        if not _initialized or not api.exchange.api_data_fully_ready(state_arg):
            return
        if api.exchange.warmup_flag(state_arg):
            return

        # Inject Setpoints
        if isinstance(setpoint, dict):
            for z, actuator_key in ZONE_SCHEDULE_NAMES.items():
                if actuator_key in _actuators:
                    val = setpoint.get(z, 24.0)
                    api.exchange.set_actuator_value(state_arg, _actuators[actuator_key], val)
        else:
            if CLG_SCHEDULE_NAME in _actuators:
                api.exchange.set_actuator_value(state_arg, _actuators[CLG_SCHEDULE_NAME], float(setpoint))

    def _on_end_zone_timestep(state_arg):
        nonlocal _global_comfort_kh, _zonal_comfort_kh
        if not _zone_handles or not api.exchange.api_data_fully_ready(state_arg):
            return
        if api.exchange.warmup_flag(state_arg):
            return
            
        dt_h = api.exchange.zone_time_step(state_arg)
        for z, h in _zone_handles.items():
            t_zone = api.exchange.get_variable_value(state_arg, h)
            # Legacy scalar tracking
            if t_zone > _thresh_global:
                _global_comfort_kh += (t_zone - _thresh_global) * dt_h
            # Granular zonal tracking
            tz_thresh = _thresh_zonal.get(z, 24.0)
            if t_zone > tz_thresh:
                _zonal_comfort_kh[z] += (t_zone - tz_thresh) * dt_h

    state = api.state_manager.new_state()
    if return_comfort or return_zonal_comfort:
        for z in OCCUPIED_ZONES:
            api.exchange.request_variable(state, "Zone Mean Air Temperature", z)
    try:
        api.runtime.callback_begin_zone_timestep_after_init_heat_balance(
            state, _on_begin_zone_timestep
        )
        if return_comfort or return_zonal_comfort:
            api.runtime.callback_end_zone_timestep_before_zone_reporting(
                state, _on_end_zone_timestep
            )

        eplus_args = [
            "-w",
            str(epw_path.resolve()),
            "-d",
            str(sim_dir.resolve()),
            "--readvars",
            str(temp_idf.resolve()),
        ]
        _prev_cwd = os.getcwd()
        os.chdir(sim_dir)
        try:
            if quiet:
                with _eplus_quiet():
                    exit_code = api.runtime.run_energyplus(state, eplus_args)
            else:
                exit_code = api.runtime.run_energyplus(state, eplus_args)
        finally:
            os.chdir(_prev_cwd)
    finally:
        api.state_manager.delete_state(state)

    if exit_code != 0:
        log.warning(f"  Simulation {candidate_id} failed (exit {exit_code})")
        return None

    hvac_kwh = _extract_hvac_energy(sim_dir)
    if hvac_kwh is None:
        return None

    if return_zonal_comfort:
        return {"hvac_kwh": hvac_kwh, "discomfort_kh": _zonal_comfort_kh}
    elif return_comfort:
        return {"hvac_kwh": hvac_kwh, "discomfort_kh": _global_comfort_kh}
    return hvac_kwh


# ── Mode: validate ────────────────────────────────────────────────────────────
def mode_validate(args, idf_path: Path, epw_path: Path) -> int:
    log.info("=== MODE: validate ===")
    api = _load_eplus_api()
    out = WORK_DIR / "validate"

    energy = run_candidate(
        api,
        idf_path,
        epw_path,
        out,
        wall_r=1.5,
        roof_r=2.5,
        orientation=0.0,
        setpoint=24.0,
        candidate_id="base_validate",
        quiet=args.quiet,
    )

    warnings, errors = _check_err(out / "base_validate")
    log.info(f"  Warnings: {warnings} | Errors: {errors}")

    if errors > 0:
        log.error(
            "  ❌ Simulation has severe errors — review eplusout.err before scaling."
        )
        return 1

    if energy is None:
        log.error("  ❌ Simulation did not return an energy value.")
        return 1

    # Exact numerical verification for reproducibility (Copilot M4 resolution)
    expected_energy = 64519.50
    tolerance = 2.0  # Safe float/OS variance floor (0.003%)
    diff = abs(energy - expected_energy)
    if diff > tolerance:
        log.error(
            f"  ❌ Energy mismatch! Expected: {expected_energy} kWh, Got: {energy} kWh "
            f"(diff: {diff:.2f} kWh > tolerance {tolerance} kWh)"
        )
        return 1
    log.info(f"  ✅ Annual HVAC Energy: {energy} kWh (Exact match within tolerance)")

    log.info("  Validation OK — ready to scale (gsa or pareto mode).")

    if args.bucket:
        stage_out(
            args.bucket,
            out / "base_validate",
            f"results/gcp_vm_validate_{datetime.now():%Y%m%d_%H%M%S}",
        )
    return 0


# ── Mode: single ──────────────────────────────────────────────────────────────
def mode_single(args, idf_path: Path, epw_path: Path) -> int:
    log.info("=== MODE: single ===")
    api = _load_eplus_api()
    out = WORK_DIR / "single"

    energy = run_candidate(
        api,
        idf_path,
        epw_path,
        out,
        wall_r=args.wall_r,
        roof_r=args.roof_r,
        orientation=float(args.orientation),
        setpoint=float(args.setpoint),
        candidate_id="single_run",
        quiet=args.quiet,
    )

    if energy is None:
        log.error("  ❌ Simulation failed.")
        return 1

    log.info(f"  ✅ Annual HVAC Energy: {energy} kWh")
    result = {
        "wall_r": args.wall_r,
        "roof_r": args.roof_r,
        "orientation": args.orientation,
        "setpoint": args.setpoint,
        "hvac_energy_kwh": energy,
    }
    (out / "single_run" / "result.json").write_text(json.dumps(result, indent=2))

    if args.bucket:
        stage_out(
            args.bucket,
            out / "single_run",
            f"results/gcp_vm_single_{datetime.now():%Y%m%d_%H%M%S}",
        )
    return 0


# ── Modo: gsa ─────────────────────────────────────────────────────────────────
def mode_gsa(args, idf_path: Path, epw_path: Path) -> int:
    """
    Global Sensitivity Analysis (Morris) on the 5 real DPs.
    Uses EnergyPlus as objective function — each evaluation = 1 annual simulation (~12s).
    N=100 trajectories × 6 perturbations = ~600 simulations × 12s ≈ 2h on VM.
    Use --n-morris 20 for quick test (~40 simulations, ~8min).
    """
    log.info("=== MODE: gsa ===")
    try:
        from SALib.analyze import morris as morris_analyze
        from SALib.sample import morris as morris_sample
    except ImportError:
        log.error("SALib not installed. Run: pip install SALib")
        return 1

    out = WORK_DIR / "gsa"
    out.mkdir(parents=True, exist_ok=True)

    # SALib problem — continuous domain (will be discretized in evaluate)
    problem = {
        "num_vars": 5,
        "names": ["wall_r", "roof_r", "glass_u", "orientation_idx", "setpoint"],
        "bounds": [
            [DP_DOMAINS["wall_r"][0], DP_DOMAINS["wall_r"][1]],
            [DP_DOMAINS["roof_r"][0], DP_DOMAINS["roof_r"][1]],
            [DP_DOMAINS["glass_u"][0], DP_DOMAINS["glass_u"][1]],
            [DP_DOMAINS["orientation"][0], DP_DOMAINS["orientation"][1]],
            [DP_DOMAINS["setpoint"][0], DP_DOMAINS["setpoint"][1]],
        ],
    }

    N = args.n_morris
    log.info(f"  Morris sampling: N={N} trajectories → ~{N * (5 + 1)} simulations")
    X = morris_sample.sample(problem, N=N, num_levels=4)
    n_samples = len(X)
    log.info(f"  Total samples: {n_samples}")

    # Build task list — keyed by sample index to preserve SALib X↔Y alignment
    tasks = []
    for i, x in enumerate(X):
        # Discretization
        wall_r_int = int(np.clip(round(x[0]), *DP_DOMAINS["wall_r"]))
        roof_r_int = int(np.clip(round(x[1]), *DP_DOMAINS["roof_r"]))
        # glass_u not yet injected in IDF (M1.6) — keep base value
        orient_idx = int(np.clip(round(x[3]), *DP_DOMAINS["orientation"]))
        setpoint_int = int(np.clip(round(x[4]), *DP_DOMAINS["setpoint"]))
        tasks.append(
            {
                "key": i,
                "idf_base": idf_path,
                "epw_path": epw_path,
                "output_dir": out,
                "wall_r": wall_r_int / 10.0,
                "roof_r": roof_r_int / 10.0,
                "orientation": ORIENTATION_MAP[orient_idx],
                "setpoint": float(setpoint_int),
                "candidate_id": f"gsa_{i:04d}",
                "quiet": args.quiet,
            }
        )

    batch = run_batch(tasks, args.workers, label="GSA", log_every=10)

    # Reassemble Y in the original sample order (X↔Y alignment for SALib)
    Y = np.full(n_samples, np.nan)
    for i in range(n_samples):
        energy = batch.get(i)
        Y[i] = energy if energy is not None else np.nan

    # Remove NaN before analysis
    valid_mask = ~np.isnan(Y)
    if valid_mask.sum() < 10:
        log.error("  Insufficient simulations for GSA analysis.")
        return 1

    Si = morris_analyze.analyze(
        problem, X[valid_mask], Y[valid_mask], num_levels=4, print_to_console=False
    )

    log.info("\n  === GSA RESULTS (Morris — μ*) ===")
    log.info(f"  {'DP':<20} {'μ* (importance)':>18} {'σ (interactions)':>16}")
    log.info("  " + "-" * 56)
    for name, mu, sigma in zip(problem["names"], Si["mu_star"], Si["sigma"]):
        log.info(f"  {name:<20} {mu:>18.4f} {sigma:>16.4f}")

    # Save results
    results = {
        "n_morris": N,
        "n_samples": n_samples,
        "n_valid": int(valid_mask.sum()),
        "mu_star": dict(zip(problem["names"], Si["mu_star"].tolist())),
        "sigma": dict(zip(problem["names"], Si["sigma"].tolist())),
    }
    results_path = out / "gsa_results.json"
    results_path.write_text(json.dumps(results, indent=2))
    log.info(f"\n  Results saved to: {results_path}")

    if args.bucket:
        stage_out(
            args.bucket, out, f"results/gcp_vm_gsa_{datetime.now():%Y%m%d_%H%M%S}"
        )
    return 0


# ── MLT Helpers (GSA-calibrated two-level proxy) ──────────────────────────────


def _fr1_proxy(setpoint: int, glass_u_int: int) -> float:
    """Comfort proxy — increases with setpoint and glass_u (higher value = worse comfort)."""
    w_sp = GSA_HIERARCHY["setpoint"]["mu_star"]
    w_gu = GSA_HIERARCHY["glass_u"]["mu_star"]
    return (w_sp * setpoint + w_gu * glass_u_int) / (w_sp + w_gu)


def _fr2_proxy(orient_idx: int, wall_r_int: int, roof_r_int: int) -> float:
    """Energy proxy — E/W orientation penalized; more insulation = lower value."""
    orient_penalty = {0: 0.0, 1: 1.0, 2: 0.5, 3: 1.0}[orient_idx]
    w_or = GSA_HIERARCHY["orientation_idx"]["mu_star"]
    w_wr = GSA_HIERARCHY["wall_r"]["mu_star"]
    w_rr = GSA_HIERARCHY["roof_r"]["mu_star"]
    norm = w_or + w_wr + w_rr
    # domain wall_r ∈ [5,25], roof_r ∈ [10,40] → max_sum = 65
    return (
        w_or * orient_penalty - (w_wr * wall_r_int + w_rr * roof_r_int) / 65.0
    ) / norm


def _pareto_filter(candidates: list[dict], f1: str, f2: str) -> list[dict]:
    """Remove dominated solutions — returns Pareto front in (f1, f2)."""
    front = []
    for c in candidates:
        if not any(
            o[f1] <= c[f1] and o[f2] <= c[f2] and (o[f1] < c[f1] or o[f2] < c[f2])
            for o in candidates
        ):
            front.append(c)
    return front


def _hypervolume_2d(front: list[dict], ref_f1: float, ref_f2: float) -> float:
    """Exact 2-D hypervolume indicator (minimisation). ref point must dominate all."""
    pts = sorted(
        [
            (c["fr1_proxy"], c["hvac_energy_kwh"])
            for c in front
            if c.get("hvac_energy_kwh") is not None
        ],
        key=lambda x: x[0],
    )
    hv, prev_f2 = 0.0, ref_f2
    for f1, f2 in pts:
        hv += (ref_f1 - f1) * (prev_f2 - f2)
        prev_f2 = f2
    return hv


def _benchmark_vs_exhaustive(p2: list[dict], ground_truth_path: Path) -> dict:
    """M3.9: compare sequential Pareto front against exhaustive ground truth."""
    data = json.loads(ground_truth_path.read_text())
    ref_front = data.get("pareto_front", [])
    ref_all = data.get("all_results", [])

    # Exact design-point match via (orient_idx, setpoint, wall_r, roof_r)
    p2_keys = {
        (c["orient_idx"], int(c["setpoint"]), c["wall_r"], c["roof_r"])
        for c in p2
        if c.get("hvac_energy_kwh") is not None
    }
    matches = sum(
        1
        for r in ref_front
        if (r["orient_idx"], int(r["setpoint"]), r["wall_r"], r["roof_r"]) in p2_keys
    )

    # Hypervolume — nadir reference from all 192 exhaustive results
    valid_all = [r for r in ref_all if r.get("hvac_energy_kwh") is not None]
    ref_f1 = max(r["fr1_proxy"] for r in valid_all) * 1.05
    ref_f2 = max(r["hvac_energy_kwh"] for r in valid_all) * 1.05
    hv_ref = _hypervolume_2d(ref_front, ref_f1, ref_f2)
    hv_seq = _hypervolume_2d(p2, ref_f1, ref_f2)

    return {
        "n_exhaustive_pareto": len(ref_front),
        "n_sequential_pareto": len(p2),
        "n_matches": matches,
        "effectiveness": round(matches / len(ref_front), 4) if ref_front else 0.0,
        "effectiveness_pct": round(matches / len(ref_front) * 100, 1) if ref_front else 0.0,
        "hv_exhaustive": round(hv_ref, 2),
        "hv_sequential": round(hv_seq, 2),
        "hv_ratio": round(hv_seq / hv_ref, 4) if hv_ref > 0 else 0.0,
        "n_exhaustive_sims": 192,
    }


# ── Mode: exhaustive ──────────────────────────────────────────────────────────
def mode_exhaustive(args, idf_path: Path, epw_path: Path) -> int:
    """
    Exhaustive search baseline over the 4 implemented DPs (M3.7).
    orient (4) × setpoint (4) × wall_r (3) × roof_r (4) = 192 simulations.

    glass_u is deferred to M1.6 — glazing is not yet injected in the IDF
    (see run_candidate / mode_gsa), so it is held constant at GLASS_U_FIXED
    and not enumerated; varying it here would produce identical simulations.
    """
    log.info("=== MODE: exhaustive ===")
    import itertools

    out = WORK_DIR / "exhaustive"
    out.mkdir(parents=True, exist_ok=True)

    orient_idxs = [0, 1, 2, 3]
    setpoints = [23.0, 24.0, 25.0, 26.0]
    wall_rs = [0.5, 1.5, 2.5]
    roof_rs = [1.0, 2.0, 3.0, 4.0]

    grid = list(itertools.product(orient_idxs, setpoints, wall_rs, roof_rs))
    n_samples = len(grid)
    log.info(
        f"  Total combinations: {n_samples} (≈ {n_samples * 30 / 3600:.1f}h on VM)"
    )

    # Build task list keyed by candidate_id
    tasks = [
        {
            "key": f"exh_{i:04d}",
            "idf_base": idf_path,
            "epw_path": epw_path,
            "output_dir": out,
            "wall_r": wr,
            "roof_r": rr,
            "orientation": ORIENTATION_MAP[o_idx],
            "setpoint": sp,
            "candidate_id": f"exh_{i:04d}",
            "quiet": args.quiet,
        }
        for i, (o_idx, sp, wr, rr) in enumerate(grid)
    ]

    batch = run_batch(tasks, args.workers, label="exhaustive", log_every=50)

    # Reassemble results in grid order (preserves all_results JSON ordering)
    results = []
    for i, (o_idx, sp, wr, rr) in enumerate(grid):
        cid = f"exh_{i:04d}"
        results.append(
            {
                "candidate_id": cid,
                "orient_idx": o_idx,
                "orientation": ORIENTATION_MAP[o_idx],
                "setpoint": sp,
                "wall_r": wr,
                "roof_r": rr,
                "glass_u": GLASS_U_FIXED,  # constant — glazing injection pending M1.6
                "fr1_proxy": _fr1_proxy(int(sp), int(GLASS_U_FIXED * 10)),
                "fr2_proxy": _fr2_proxy(o_idx, int(wr * 10), int(rr * 10)),
                "hvac_energy_kwh": batch.get(cid),
            }
        )

    valid = [c for c in results if c.get("hvac_energy_kwh") is not None]
    pareto = _pareto_filter(valid, "fr1_proxy", "hvac_energy_kwh")
    pareto.sort(key=lambda c: c["fr1_proxy"])

    out_data = {
        "n_samples": n_samples,
        "n_valid": len(valid),
        "all_results": results,
        "pareto_front": pareto,
    }

    results_path = out / "exhaustive_pareto.json"
    results_path.write_text(json.dumps(out_data, indent=2))
    log.info(f"\n  Results saved to: {results_path}")

    if args.bucket:
        stage_out(
            args.bucket,
            out,
            f"results/gcp_vm_exhaustive_{datetime.now():%Y%m%d_%H%M%S}",
        )
    return 0


# ── Mode: calibrate ───────────────────────────────────────────────────────────
# Coarse grids for wall_r / roof_r — keep Variant B marginal tables tractable.
CALIB_WALL_R_GRID = [0.5, 1.0, 1.5, 2.0, 2.5]
CALIB_ROOF_R_GRID = [1.0, 1.75, 2.5, 3.25, 4.0]


def mode_calibrate(args, idf_path: Path, epw_path: Path) -> int:
    """
    Dedicated calibration run (M3.1) — LHS sample over the 4 implemented DPs and
    evaluate each with EnergyPlus, producing the dataset that `scripts/build_proxies.py`
    fits the two CP-SAT linearization proxies from (Variant A regression / Variant B
    marginal tables). Independent of the M3.7 exhaustive ground truth.

    DPs: orientation_idx, setpoint, wall_r, roof_r (glass_u excluded — inert, M1.6).
    """
    log.info("=== MODE: calibrate ===")
    from SALib.sample import latin

    out = WORK_DIR / "calibrate"
    out.mkdir(parents=True, exist_ok=True)

    # LHS over grid-index space; bounds are index ranges, snapped to discrete grids.
    problem = {
        "num_vars": 4,
        "names": ["orient_idx", "setpoint", "wall_r_i", "roof_r_i"],
        "bounds": [[0, 3], [23, 26], [0, 4], [0, 4]],
    }
    raw = latin.sample(problem, args.n_calibrate)

    def _snap(row) -> tuple:
        o = int(round(min(max(row[0], 0), 3)))
        sp = int(round(min(max(row[1], 23), 26)))
        wr = CALIB_WALL_R_GRID[int(round(min(max(row[2], 0), 4)))]
        rr = CALIB_ROOF_R_GRID[int(round(min(max(row[3], 0), 4)))]
        return (o, sp, wr, rr)

    samples = sorted({_snap(r) for r in raw})
    log.info(
        f"  {len(samples)} unique calibration points "
        f"(from {args.n_calibrate} LHS draws) ≈ {len(samples) * 30 / 60:.0f} min"
    )

    # Build task list keyed by candidate_id
    tasks = [
        {
            "key": f"cal_{i:04d}",
            "idf_base": idf_path,
            "epw_path": epw_path,
            "output_dir": out,
            "wall_r": wr,
            "roof_r": rr,
            "orientation": ORIENTATION_MAP[o_idx],
            "setpoint": float(sp),
            "candidate_id": f"cal_{i:04d}",
            "quiet": args.quiet,
        }
        for i, (o_idx, sp, wr, rr) in enumerate(samples)
    ]

    batch = run_batch(tasks, args.workers, label="calibrate", log_every=25)

    # Reassemble results in sample order (preserves samples JSON ordering)
    results = []
    for i, (o_idx, sp, wr, rr) in enumerate(samples):
        cid = f"cal_{i:04d}"
        results.append(
            {
                "candidate_id": cid,
                "orient_idx": o_idx,
                "orientation": ORIENTATION_MAP[o_idx],
                "setpoint": sp,
                "wall_r": wr,
                "roof_r": rr,
                "hvac_energy_kwh": batch.get(cid),
            }
        )

    valid = [r for r in results if r.get("hvac_energy_kwh") is not None]
    out_data = {
        "n_samples": len(samples),
        "n_valid": len(valid),
        "wall_r_grid": CALIB_WALL_R_GRID,
        "roof_r_grid": CALIB_ROOF_R_GRID,
        "samples": results,
    }
    results_path = out / "calibration_samples.json"
    results_path.write_text(json.dumps(out_data, indent=2))
    log.info(f"\n  {len(valid)}/{len(samples)} valid — saved to: {results_path}")

    if args.bucket:
        stage_out(
            args.bucket,
            out,
            f"results/gcp_vm_calibrate_{datetime.now():%Y%m%d_%H%M%S}",
        )
    return 0


# ── Modo: pareto ──────────────────────────────────────────────────────────────
def mode_pareto(args, idf_path: Path, epw_path: Path) -> int:
    """
    Calibrated MLT two-level pipeline.

    Level 1 (FR1 proxy — comfort): grid orientation_idx × setpoint (16 combos)
    Level 2 (FR2 proxy — energy):  CP-SAT for wall_r/roof_r — objective from one of
                two calibrated linearization variants (--linearization, M3.3):
                regression (signed coefficients) or marginal (additive tables).
    EnergyPlus: real simulation on candidates passing FR1 filter
    Pareto:     non-dominated front in (fr1_proxy, hvac_energy_kwh)
    """
    log.info("=== MODE: pareto (MLT two-level) ===")
    try:
        from ortools.sat.python import cp_model
    except ImportError:
        log.error("ortools not installed. Run: pip install ortools")
        return 1

    out = WORK_DIR / "pareto"
    out.mkdir(parents=True, exist_ok=True)

    # ── Linearization proxy (M3.3) — calibrated, replaces hand-tuned weights ──
    # The CP-SAT objective comes from one of two variants calibrated by
    # scripts/build_proxies.py (from `run_simulation.py --mode calibrate`). Both
    # predict EnergyPlus annual HVAC energy → CP-SAT MINIMIZES it. No manual sign
    # reasoning: the regression derives signed coefficients automatically; the
    # marginal tables are additive lookups. glass_u is excluded (inert until the
    # glazing injection of M1.6 — see _fr1_proxy below, held at GLASS_U_FIXED).
    data_dir = Path(__file__).resolve().parent / "data"
    SCALE = 100

    if args.linearization == "regression":
        proxy = json.loads(
            (data_dir / "proxy_coefficients.json").read_text(encoding="utf-8")
        )
        # energy ≈ Σ aᵢ·xᵢ + b; wall_r value = dp_wall_r/10 → coef per dp unit = aᵢ/10
        obj_wall = round(proxy["coefficients_kwh"]["wall_r"] / 10.0 * SCALE)
        obj_roof = round(proxy["coefficients_kwh"]["roof_r"] / 10.0 * SCALE)
        log.info(
            f"  Linearization: regression "
            f"(R²={proxy['fit']['r2']}, MAE={proxy['fit']['mae_kwh']} kWh)"
        )
    else:  # marginal
        import numpy as np

        proxy = json.loads(
            (data_dir / "marginal_tables.json").read_text(encoding="utf-8")
        )
        # Interpolate the 5-point coarse tables onto the fine CP-SAT int domains.
        wall_tbl = [
            int(round(v))
            for v in np.interp(
                [d / 10.0 for d in range(5, 26)],
                proxy["values"]["wall_r"],
                proxy["table_int"]["wall_r"],
            )
        ]
        roof_tbl = [
            int(round(v))
            for v in np.interp(
                [d / 10.0 for d in range(10, 41)],
                proxy["values"]["roof_r"],
                proxy["table_int"]["roof_r"],
            )
        ]
        log.info(
            f"  Linearization: marginal "
            f"(additive MAE={proxy['fit']['additive_mae_kwh']} kWh)"
        )

    # ── Level 1: grid orientation × setpoint × budget ────────────────────────
    all_candidates = []
    budgets = [int(b.strip()) for b in args.pareto_budgets.split(",")]
    seen_configs: set[tuple] = set()  # deduplicates identical candidates

    for orient_idx in range(4):
        for setpoint in range(23, 27):
            for budget in budgets:
                model = cp_model.CpModel()
                dp_wall_r = model.NewIntVar(5, 25, "wall_r")
                dp_roof_r = model.NewIntVar(10, 40, "roof_r")

                # Axiomatic constraint: roof ≥ wall
                model.Add(dp_roof_r >= dp_wall_r)

                # Budget constraint: explores cost×energy trade-off
                model.Add(dp_wall_r + dp_roof_r <= budget)

                # Objective: MINIMIZE the calibrated energy proxy (M3.3)
                if args.linearization == "regression":
                    model.Minimize(obj_wall * dp_wall_r + obj_roof * dp_roof_r)
                else:  # marginal — additive table lookup
                    e_wall = model.NewIntVar(min(wall_tbl), max(wall_tbl), "e_wall")
                    e_roof = model.NewIntVar(min(roof_tbl), max(roof_tbl), "e_roof")
                    model.AddElement(dp_wall_r - 5, wall_tbl, e_wall)
                    model.AddElement(dp_roof_r - 10, roof_tbl, e_roof)
                    model.Minimize(e_wall + e_roof)

                solver = cp_model.CpSolver()
                status = solver.Solve(model)

                if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                    log.warning(
                        f"  CP-SAT infeasible: orient={orient_idx}, sp={setpoint}, budget={budget}"
                    )
                    continue

                wall_r_int = solver.Value(dp_wall_r)
                roof_r_int = solver.Value(dp_roof_r)

                # Discard duplicate configurations (larger budgets collapse to same point)
                key = (orient_idx, setpoint, wall_r_int, roof_r_int)
                if key in seen_configs:
                    continue
                seen_configs.add(key)

                all_candidates.append(
                    {
                        "candidate_id": f"MLT_o{orient_idx}_s{setpoint}_b{budget}",
                        "orient_idx": orient_idx,
                        "orientation": ORIENTATION_MAP[orient_idx],
                        "setpoint": setpoint,
                        "budget": budget,
                        "wall_r": wall_r_int / 10.0,
                        "roof_r": roof_r_int / 10.0,
                        "glass_u": GLASS_U_FIXED,  # constant — glazing injection pending M1.6
                        "fr1_proxy": _fr1_proxy(setpoint, int(GLASS_U_FIXED * 10)),
                        "fr2_proxy": _fr2_proxy(orient_idx, wall_r_int, roof_r_int),
                    }
                )

    log.info(
        f"  {len(all_candidates)} unique candidates (grid {4}×{4}×{len(budgets)} budgets)"
    )

    # ── FR1 filter: keep FR1 ≤ FR1_opt + ε ──────────────────────────────────
    # ε = N setpoint steps above optimum (default: 3 → includes all 4 setpoints)
    fr1_values = sorted(set(c["fr1_proxy"] for c in all_candidates))
    fr1_opt = fr1_values[0]
    fr1_step = (
        min(b - a for a, b in zip(fr1_values, fr1_values[1:]))
        if len(fr1_values) > 1
        else 0.0
    )
    epsilon = fr1_step * args.epsilon_steps
    level2 = [c for c in all_candidates if c["fr1_proxy"] <= fr1_opt + epsilon]
    log.info(
        f"  FR1_opt={fr1_opt:.3f}  fr1_step={fr1_step:.3f}  "
        f"ε={epsilon:.3f} ({args.epsilon_steps} steps)  → {len(level2)} candidates pass FR1 filter"
    )

    # ── EnergyPlus: real FR2 on filtered candidates ───────────────────────────
    tasks = [
        {
            "key": c["candidate_id"],
            "idf_base": idf_path,
            "epw_path": epw_path,
            "output_dir": out,
            "wall_r": c["wall_r"],
            "roof_r": c["roof_r"],
            "orientation": c["orientation"],
            "setpoint": float(c["setpoint"]),
            "candidate_id": c["candidate_id"],
            "quiet": args.quiet,
        }
        for c in level2
    ]

    batch = run_batch(tasks, args.workers, label="pareto", log_every=5)

    for c in level2:
        c["hvac_energy_kwh"] = batch.get(c["candidate_id"])
        log.info(
            f"  {c['candidate_id']}: setpoint={c['setpoint']}°C | "
            f"orient={c['orientation']}° | wall_r={c['wall_r']} | "
            f"roof_r={c['roof_r']} | HVAC={c['hvac_energy_kwh']} kWh"
        )

    # ── Real Pareto front ─────────────────────────────────────────────────────
    valid = [c for c in level2 if c.get("hvac_energy_kwh") is not None]
    pareto = _pareto_filter(valid, "fr1_proxy", "hvac_energy_kwh")
    pareto.sort(key=lambda c: c["fr1_proxy"])

    if not pareto:
        log.error("  ❌ No valid EnergyPlus candidates — all simulations failed.")
        return 1

    log.info(f"\n  Pareto front: {len(pareto)} non-dominated solutions")
    for c in pareto:
        log.info(
            f"    {c['candidate_id']}: FR1={c['fr1_proxy']:.3f} | "
            f"FR2={c['hvac_energy_kwh']:.1f} kWh | orient={c['orientation']}°"
        )

    results_path = out / "pareto_front.json"
    results_path.write_text(
        json.dumps(
            {
                "linearization": args.linearization,
                "all_candidates": all_candidates,
                "level2_filtered": level2,
                "pareto_front": pareto,
                "fr1_opt": fr1_opt,
                "epsilon": epsilon,
            },
            indent=2,
        )
    )
    log.info(f"  Results saved to: {results_path}")

    if args.bucket:
        stage_out(
            args.bucket, out, f"results/gcp_vm_pareto_{datetime.now():%Y%m%d_%H%M%S}"
        )
    return 0


# ── Mode: gsa-sobol ───────────────────────────────────────────────────────────
def mode_gsa_sobol(args, idf_path: Path, epw_path: Path) -> int:
    """M2: Sobol GSA with both FR1 (discomfort degree-hours) and FR2 (HVAC energy).

    Saltelli N × (k+2) EnergyPlus runs → S1/ST indices per DP per FR →
    data/gsa_samples.csv, data/gsa_results.csv, data/gsa_results.json,
    data/figures/figure1_sobol_heatmap.{png,svg}.

    Checkpoints: samples CSV is written before the batch; results CSV is written
    after the batch. A crashed mid-run can resume by re-running (re-samples with
    same seed, results are recomputed).
    """
    log.info("=== MODE: gsa-sobol ===")
    from src.pre_processing.gsa_sampler import PROBLEM, n_sims, sample_saltelli, save_samples
    from src.pre_processing.gsa_runner import build_tasks, run_and_collect, save_results
    from src.analysis import gsa_analysis, ad_matrix

    data_dir = Path(__file__).resolve().parent / "data"
    samples_path = data_dir / "gsa_samples.csv"
    results_path = data_dir / "gsa_results.csv"
    sobol_path = data_dir / "gsa_results.json"
    ad_path = data_dir / "gsa_ad_matrix.json"

    N = args.n_sobol
    seed = args.seed
    total = n_sims(N)
    log.info(f"  Saltelli N={N}, seed={seed} → {total} simulations")
    log.info(f"  Workers: {args.workers}")

    # Step 1: Sample
    X = sample_saltelli(N=N, seed=seed)
    save_samples(X, samples_path)
    log.info(f"  Samples saved → {samples_path}")

    # Step 2: Run batch
    out = WORK_DIR / "gsa_sobol"
    out.mkdir(parents=True, exist_ok=True)
    # comfort_threshold=24.0 ensures FR1 = Σ max(0, T - 24°C) Δt (manuscript §2.1).
    # Without it, gsa_runner defaults to dynamic setpoint as threshold, which
    # measures tracking-error and inverts the sign of corr(setpoint, FR1).
    tasks = build_tasks(X, idf_path, epw_path, out, quiet=args.quiet,
                        comfort_threshold=24.0)
    df = run_and_collect(tasks, args.workers, chunk_size=args.chunk_size)
    save_results(df, results_path)

    # Check for failures before Sobol analysis
    n_nan = int(df[["fr1_kh", "fr2_kwh"]].isna().any(axis=1).sum())
    if n_nan > 0:
        log.error(
            f"  {n_nan}/{total} sims failed — cannot compute Sobol indices. "
            "Investigate failures and re-run."
        )
        return 1

    # Step 3: Sobol analysis
    import numpy as np
    Y_fr1 = df["fr1_kh"].to_numpy()
    Y_fr2 = df["fr2_kwh"].to_numpy()
    sobol_result = gsa_analysis.analyze_both(
        Y_fr1, Y_fr2, PROBLEM, n_samples=N, out_path=sobol_path
    )

    # Step 4: AD matrix
    ad_result = ad_matrix.build_ad_matrix(sobol_result)
    ad_matrix.save(ad_result, ad_path)

    # Step 5: Figure 1
    try:
        import subprocess
        fig_script = Path(__file__).resolve().parent / "scripts" / "generate_figure1.py"
        subprocess.run(
            [sys.executable, str(fig_script), "--input", str(sobol_path)],
            check=True,
        )
    except Exception as e:
        log.warning(f"  Figure 1 generation failed (non-fatal): {e}")

    if args.bucket:
        stage_out(
            args.bucket, out, f"results/gcp_vm_gsa_sobol_{datetime.now():%Y%m%d_%H%M%S}"
        )
        for p in (samples_path, results_path, sobol_path, ad_path):
            if p.exists():
                stage_out(args.bucket, p.parent, f"results/gsa_sobol_data")

    log.info("  ✅ gsa-sobol complete.")
    return 0


# ── Mode: pareto-sequential ───────────────────────────────────────────────────
def mode_pareto_sequential(args, idf_path: Path, epw_path: Path) -> int:
    """
    M3.8: Talami (2025) sequential block-propagation Pareto search.

    Block 1 — Geometry/HVAC (orient × setpoint, 4×4 = 16 sims):
        wall_r and roof_r are fixed at baseline. Extract Pareto front P1.

    Block 2 — Envelope (wall_r × roof_r per P1 solution, |P1|×3×4 sims):
        Only propagate the Pareto-optimal solutions from Block 1.
        Extract global Pareto front P2.

    M3.9 benchmark: compare P2 against the M3.7 exhaustive ground truth
    (192 sims) and report effectiveness, hypervolume ratio, and sim reduction.
    """
    import itertools

    log.info("=== MODE: pareto-sequential (M3.8 — Talami 2025) ===")

    out = WORK_DIR / "pareto_sequential"
    out.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # DP grids (same discrete values as mode_exhaustive)
    orient_idxs = [0, 1, 2, 3]
    setpoints    = [23.0, 24.0, 25.0, 26.0]
    wall_rs      = [0.5, 1.5, 2.5]
    roof_rs      = [1.0, 2.0, 3.0, 4.0]

    # Baseline envelope values used while Block 1 sweeps geometry/HVAC.
    # Both must be on the exhaustive grid so Block-1 (orient, setpoint) results
    # share the same physical configuration as the corresponding slice of the
    # M3.7 exhaustive grid — required for unbiased M3.9 effectiveness scoring.
    WALL_R_BASE = 1.5   # ∈ wall_rs = [0.5, 1.5, 2.5] (median)
    ROOF_R_BASE = 2.0   # ∈ roof_rs = [1.0, 2.0, 3.0, 4.0] (on-grid; was 2.5 off-grid)

    # ── Block 1: orient × setpoint (envelope fixed at baseline) ──────────────
    log.info(f"\n  Block 1: {len(orient_idxs)}×{len(setpoints)} = "
             f"{len(orient_idxs)*len(setpoints)} sims  "
             f"(wall_r={WALL_R_BASE}, roof_r={ROOF_R_BASE} fixed)")

    tasks_b1 = [
        {
            "key": f"seq_b1_o{o}_s{int(sp)}",
            "idf_base": idf_path,
            "epw_path": epw_path,
            "output_dir": out,
            "wall_r": WALL_R_BASE,
            "roof_r": ROOF_R_BASE,
            "orientation": ORIENTATION_MAP[o],
            "setpoint": sp,
            "candidate_id": f"seq_b1_o{o}_s{int(sp)}",
            "quiet": args.quiet,
            # extra fields for result assembly (not used by run_candidate)
            "_orient_idx": o,
        }
        for o, sp in itertools.product(orient_idxs, setpoints)
    ]

    batch_b1 = run_batch(tasks_b1, args.workers, label="seq-block1", log_every=4)

    results_b1 = []
    for t in tasks_b1:
        energy = batch_b1.get(t["key"])
        results_b1.append(
            {
                "candidate_id": t["key"],
                "orient_idx": t["_orient_idx"],
                "orientation": t["orientation"],
                "setpoint": t["setpoint"],
                "wall_r": WALL_R_BASE,
                "roof_r": ROOF_R_BASE,
                "glass_u": GLASS_U_FIXED,
                "fr1_proxy": _fr1_proxy(int(t["setpoint"]), int(GLASS_U_FIXED * 10)),
                "fr2_proxy": _fr2_proxy(
                    t["_orient_idx"], int(WALL_R_BASE * 10), int(ROOF_R_BASE * 10)
                ),
                "hvac_energy_kwh": energy,
            }
        )

    valid_b1 = [c for c in results_b1 if c["hvac_energy_kwh"] is not None]
    p1 = _pareto_filter(valid_b1, "fr1_proxy", "hvac_energy_kwh")
    p1.sort(key=lambda c: c["fr1_proxy"])
    log.info(f"  Block 1 valid: {len(valid_b1)}/{len(tasks_b1)}  |P1| = {len(p1)}")

    # ── Block 2: wall_r × roof_r for each P1 solution ────────────────────────
    n_b2 = len(p1) * len(wall_rs) * len(roof_rs)
    log.info(f"\n  Block 2: |P1|={len(p1)} × {len(wall_rs)}×{len(roof_rs)} = {n_b2} sims")

    tasks_b2 = [
        {
            "key": f"seq_b2_o{p['orient_idx']}_s{int(p['setpoint'])}"
                   f"_w{int(wr*10)}_r{int(rr*10)}",
            "idf_base": idf_path,
            "epw_path": epw_path,
            "output_dir": out,
            "wall_r": wr,
            "roof_r": rr,
            "orientation": p["orientation"],
            "setpoint": p["setpoint"],
            "candidate_id": f"seq_b2_o{p['orient_idx']}_s{int(p['setpoint'])}"
                            f"_w{int(wr*10)}_r{int(rr*10)}",
            "quiet": args.quiet,
            "_orient_idx": p["orient_idx"],
        }
        for p, wr, rr in itertools.product(p1, wall_rs, roof_rs)
    ]

    batch_b2 = run_batch(tasks_b2, args.workers, label="seq-block2", log_every=10)

    results_b2 = []
    for t in tasks_b2:
        energy = batch_b2.get(t["key"])
        results_b2.append(
            {
                "candidate_id": t["key"],
                "orient_idx": t["_orient_idx"],
                "orientation": t["orientation"],
                "setpoint": t["setpoint"],
                "wall_r": t["wall_r"],
                "roof_r": t["roof_r"],
                "glass_u": GLASS_U_FIXED,
                "fr1_proxy": _fr1_proxy(int(t["setpoint"]), int(GLASS_U_FIXED * 10)),
                "fr2_proxy": _fr2_proxy(
                    t["_orient_idx"], int(t["wall_r"] * 10), int(t["roof_r"] * 10)
                ),
                "hvac_energy_kwh": energy,
            }
        )

    valid_b2 = [c for c in results_b2 if c["hvac_energy_kwh"] is not None]
    p2 = _pareto_filter(valid_b2, "fr1_proxy", "hvac_energy_kwh")
    p2.sort(key=lambda c: c["fr1_proxy"])
    log.info(f"  Block 2 valid: {len(valid_b2)}/{len(tasks_b2)}  |P2| = {len(p2)}")

    n_total = len(tasks_b1) + len(tasks_b2)
    log.info(f"\n  Total sims: {n_total}  (vs 192 exhaustive — "
             f"{round((1 - n_total/192)*100, 1)}% reduction)")

    # ── M3.9 Benchmark ────────────────────────────────────────────────────────
    benchmark: dict = {}
    gt_local = WORK_DIR / "exhaustive" / "exhaustive_pareto_ref.json"
    if args.bucket and not args.no_gcs:
        stage_in(args.bucket, args.ground_truth_gcs, gt_local)

    # Fallback: in --no-gcs mode, look for the shipped copy under data/
    # so a clean clone reproduces the benchmark without manual staging.
    if not gt_local.exists():
        shipped_gt = Path(__file__).resolve().parent / "data" / "exhaustive_pareto_ref.json"
        if shipped_gt.exists():
            gt_local = shipped_gt

    if gt_local.exists():
        benchmark = _benchmark_vs_exhaustive(p2, gt_local)
        benchmark["n_sequential_total_sims"] = n_total
        benchmark["sim_reduction_pct"] = round((1 - n_total / 192) * 100, 1)
        log.info(
            f"\n  === M3.9 Benchmark ===\n"
            f"  Effectiveness : {benchmark['effectiveness_pct']}%"
            f"  (target ≥ 95%)\n"
            f"  HV ratio      : {benchmark['hv_ratio']}"
            f"  (target ≥ 0.90)\n"
            f"  Sims used     : {n_total} / 192"
            f"  ({benchmark['sim_reduction_pct']}% reduction)"
        )
    else:
        log.warning("  Ground-truth file not found — M3.9 benchmark skipped.")
        log.warning(f"  Expected at: {gt_local}")

    # ── Output ────────────────────────────────────────────────────────────────
    out_data = {
        "n_block1_sims": len(tasks_b1),
        "n_block2_sims": len(tasks_b2),
        "n_total_sims": n_total,
        "n_p1": len(p1),
        "n_p2": len(p2),
        "block1_results": results_b1,
        "block1_pareto": p1,
        "block2_results": results_b2,
        "pareto_front": p2,
        "benchmark": benchmark,
    }

    results_path = out / f"pareto_sequential_{ts}.json"
    results_path.write_text(json.dumps(out_data, indent=2))
    log.info(f"\n  Results saved: {results_path}")

    # Markdown summary (M3.9 report — copy to plan/cpsat_validation_XXXX.md)
    if benchmark:
        md = (
            f"# M3.9 Benchmark — Pareto Sequential vs Exhaustive\n\n"
            f"**Run:** {ts}\n\n"
            f"| Metric | Value | Target |\n"
            f"|---|---|---|\n"
            f"| Effectiveness | {benchmark['effectiveness_pct']}% | ≥ 95% |\n"
            f"| HV ratio | {benchmark['hv_ratio']} | ≥ 0.90 |\n"
            f"| Sequential sims | {n_total} | — |\n"
            f"| Exhaustive sims | 192 | — |\n"
            f"| Sim reduction | {benchmark['sim_reduction_pct']}% | — |\n"
            f"| Matches (|P2∩P_ref|) | {benchmark['n_matches']} / "
            f"{benchmark['n_exhaustive_pareto']} | — |\n\n"
            f"## Pareto Front P2 ({len(p2)} points)\n\n"
            f"| orient_idx | setpoint | wall_r | roof_r | FR1 proxy | HVAC energy (kWh) |\n"
            f"|---|---|---|---|---|---|\n"
        )
        for c in p2:
            md += (
                f"| {c['orient_idx']} | {c['setpoint']} | {c['wall_r']} "
                f"| {c['roof_r']} | {c['fr1_proxy']:.4f} "
                f"| {c['hvac_energy_kwh']:.1f} |\n"
            )
        md_path = out / f"pareto_sequential_benchmark_{ts}.md"
        md_path.write_text(md)
        log.info(f"  Benchmark MD : {md_path}")

    if args.bucket:
        stage_out(args.bucket, out, f"results/gcp_vm_pareto_sequential_{ts}")

    return 0


# ── Main ──────────────────────────────────────────────────────────────────────
_BATCH_MODES = {"gsa", "pareto", "pareto-sequential", "exhaustive", "calibrate"}


def _should_check_billing(args) -> bool:
    """Resolve the auto-billing toggle: explicit flag wins; else default by mode."""
    if args.check_billing is not None:
        return args.check_billing
    return args.mode in _BATCH_MODES


def _run_billing_check(args) -> None:
    """Invoke scripts/check_billing.py at the end of a successful batch run.

    Non-fatal — logs warnings on failure but never raises. Skipped gracefully
    when the script is absent (e.g. stale public-scope VMs that pre-date the
    billing tooling).
    """
    script = Path(__file__).resolve().parent / "scripts" / "check_billing.py"
    if not script.exists():
        log.warning(f"  Billing check skipped: {script} not present on this VM")
        return

    log.info("\n=== Billing check (post-run snapshot) ===")
    cmd = [sys.executable, "-u", str(script), "--today"]
    if args.bucket and not args.no_gcs:
        cmd.append("--stage-out")

    try:
        result = subprocess.run(cmd, timeout=120, check=False)
        if result.returncode == 0:
            log.info("  Billing snapshot OK")
        else:
            log.warning(f"  check_billing.py exited {result.returncode} (non-fatal)")
    except subprocess.TimeoutExpired:
        log.warning("  Billing check timed out after 120s (non-fatal)")
    except Exception as e:
        log.warning(f"  Billing check failed: {e} (non-fatal)")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="EnergyPlus GCP VM Standalone Runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--mode",
        choices=["validate", "single", "gsa", "gsa-sobol", "pareto", "exhaustive", "calibrate", "pareto-sequential"],
        default="validate",
        help="Execution mode (default: validate)",
    )
    parser.add_argument(
        "--bucket", default=DEFAULT_BUCKET, help="GCS bucket for Stage-In/Stage-Out"
    )
    parser.add_argument("--idf", default=DEFAULT_IDF_GCS, help="IDF path in GCS bucket")
    parser.add_argument("--epw", default=DEFAULT_EPW_GCS, help="EPW path in GCS bucket")
    parser.add_argument(
        "--no-gcs",
        action="store_true",
        help="Use local files (no GCS — local development)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress verbose EnergyPlus output (Warming up, Initializing...)",
    )
    parser.add_argument(
        "--scratch-dir",
        type=str,
        default="/tmp/energyplus_sim",
        help="Directory for temporary simulation files (e.g. /mnt/local_ssd)",
    )
    parser.add_argument(
        "--auto-shutdown",
        action="store_true",
        help="Shut down the VM via sudo poweroff when the run finishes.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=os.cpu_count() or 1,
        help="Parallel worker processes for batch modes "
        "(gsa/exhaustive/calibrate/pareto). Default: os.cpu_count(). "
        "Use --workers 1 for the sequential baseline.",
    )

    # single mode
    parser.add_argument("--wall-r", type=float, default=1.5)
    parser.add_argument("--roof-r", type=float, default=2.5)
    parser.add_argument("--orientation", type=int, default=0, choices=[0, 90, 180, 270])
    parser.add_argument("--setpoint", type=int, default=24, choices=[23, 24, 25, 26])

    # gsa mode
    parser.add_argument(
        "--n-morris",
        type=int,
        default=50,
        help="Morris trajectories (default: 50 ≈ 300 simulations, ~1h)",
    )

    # calibrate mode
    parser.add_argument(
        "--n-calibrate",
        type=int,
        default=80,
        help="Calibration run draws: LHS N (default: 80), or Saltelli base N "
             "(total sims = N*(k+2) = N*9 for 7 vars; default 80 → 720 rows)",
    )
    # gsa-sobol mode (M2)
    parser.add_argument(
        "--n-sobol",
        type=int,
        default=128,
        help="Saltelli base N for gsa-sobol mode. Total sims = N*(k+2) "
             "(default: 128 → 768 sims for 4 DPs)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for Saltelli sampling (gsa-sobol). Pinning this is "
             "required for reproducible Sobol indices (default: 42).",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=256,
        help="Sim dir cleanup chunk size for gsa-sobol mode (default: 256)",
    )

    # pareto mode
    parser.add_argument(
        "--pareto-budgets",
        default="30,40,50,65",
        help="CP-SAT insulation budgets: wall_r_int+roof_r_int ≤ budget (default: '30,40,50,65')",
    )
    parser.add_argument(
        "--epsilon-steps",
        type=int,
        default=3,
        help="Setpoint steps allowed above FR1_opt in Level-1 filter (default: 3 = all setpoints)",
    )
    parser.add_argument(
        "--linearization",
        choices=["regression", "marginal"],
        default="regression",
        help="CP-SAT energy proxy (M3.3): regression (signed coefs) or marginal (tables)",
    )


    # pareto-sequential mode (M3.8)
    parser.add_argument(
        "--ground-truth-gcs",
        default="results/gcp_vm_exhaustive_20260521_132720/exhaustive_pareto.json",
        help="GCS path to M3.7 exhaustive_pareto.json used for M3.9 benchmark "
             "(default: the 192-sim run from 2026-05-21)",
    )

    # Auto-billing snapshot after batch runs
    parser.add_argument(
        "--check-billing",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="After a successful run, invoke scripts/check_billing.py --today and "
             "stage the CSV to GCS. Default: ON for batch modes "
             "(gsa/pareto/pareto-sequential/exhaustive/calibrate), OFF for "
             "validate/single. Use --no-check-billing to disable.",
    )

    args = parser.parse_args()
    if args.workers < 1:
        args.workers = 1

    global WORK_DIR
    WORK_DIR = Path(args.scratch_dir)
    
    # If using a custom scratch directory like an NVMe mount, wait for the background
    # startup script (mkfs + mount + chmod) to finish making it writable.
    if str(WORK_DIR) != "/tmp/energyplus_sim":
        import time
        retries = 30
        while retries > 0:
            if WORK_DIR.exists() and os.access(WORK_DIR, os.W_OK):
                break
            if not WORK_DIR.exists() and os.access(WORK_DIR.parent, os.W_OK):
                break
            log.info(f"Waiting for {WORK_DIR} to become writable (startup script running)...")
            time.sleep(2)
            retries -= 1


    log.info(f"EnergyPlus dir : {EPLUS_DIR}")
    log.info(f"Mode           : {args.mode}")
    log.info(f"GCS Bucket     : {args.bucket}")

    # Stage-In: download IDF and EPW from GCS (or use local)
    idf_local = WORK_DIR / "inputs" / Path(args.idf).name
    epw_local = WORK_DIR / "inputs" / Path(args.epw).name

    if args.no_gcs:
        # Local development — use files from data/ and skip all GCS Stage-Out
        args.bucket = None
        project_root = Path(__file__).resolve().parent
        idf_local = project_root / "data" / "5ZoneAirCooled_Opt.idf"
        epw_local = (
            project_root / "data" / "USA_IL_Chicago-OHare.Intl.AP.725300_TMY3.epw"
        )
        if not idf_local.exists() or not epw_local.exists():
            log.error(f"Local files not found at {project_root / 'data'}")
            return 1
    else:
        log.info("Stage-In: downloading inputs from GCS...")
        ok_idf = stage_in(args.bucket, args.idf, idf_local)
        ok_epw = stage_in(args.bucket, args.epw, epw_local)
        if not ok_idf or not ok_epw:
            log.error("Stage-In failed. Use --no-gcs for local development.")
            return 1

    # Dispatch by mode
    dispatch = {
        "validate": mode_validate,
        "single": mode_single,
        "gsa": mode_gsa,
        "gsa-sobol": mode_gsa_sobol,
        "pareto": mode_pareto,
        "exhaustive": mode_exhaustive,
        "calibrate": mode_calibrate,
        "pareto-sequential": mode_pareto_sequential,
    }
    rc = 1
    try:
        rc = dispatch[args.mode](args, idf_local, epw_local)

        if rc == 0 and _should_check_billing(args):
            _run_billing_check(args)
    finally:
        if args.auto_shutdown:
            log.info("=== HPC Auto-Shutdown: turning off VM in 5 seconds ===")
            import time
            time.sleep(5)
            os.system("sudo poweroff")

    return rc


if __name__ == "__main__":
    sys.exit(main())
