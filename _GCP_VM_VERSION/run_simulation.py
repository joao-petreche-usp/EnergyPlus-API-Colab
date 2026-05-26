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
    CLG_SCHEDULE_NAME,
    DP_DOMAINS,
    EPLUS_DIR,
    K_IN02,
    K_IN46,
    ORIENTATION_MAP,
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
    setpoint: float,  # °C (ex: 24.0)
    candidate_id: str = "candidate",
    quiet: bool = False,
) -> Optional[float]:
    """
    Run 1 EnergyPlus simulation with the provided DPs.
    Returns annual HVAC energy (kWh) or None on failure.
    """
    from src.utils.idf_patcher import IDFPatcher

    sim_dir = output_dir / candidate_id
    sim_dir.mkdir(parents=True, exist_ok=True)
    temp_idf = sim_dir / f"model_{candidate_id}.idf"

    # 1. Pre-processing: patch IDF (geometry + envelope)
    patcher = IDFPatcher(idf_base)
    patcher.reset()
    patcher.set_north_axis(orientation)
    patcher.set_material_thickness("IN02", wall_r * K_IN02)
    patcher.set_material_thickness("IN46", roof_r * K_IN46)  # M1.4
    # glazing (M1.6) — not yet implemented in base IDF
    patcher.save(temp_idf)

    # 2. Runtime: setpoint injection via Exchange API (closure over `setpoint`)
    _initialized = False
    _actuator = -1

    def _on_begin_zone_timestep(state_arg):
        nonlocal _initialized, _actuator
        if not _initialized and api.exchange.api_data_fully_ready(state_arg):
            _actuator = api.exchange.get_actuator_handle(
                state_arg, "Schedule:Compact", "Schedule Value", CLG_SCHEDULE_NAME
            )
            _initialized = True
        if not _initialized or not api.exchange.api_data_fully_ready(state_arg):
            return
        if api.exchange.warmup_flag(state_arg):
            return
        if _actuator != -1:
            api.exchange.set_actuator_value(state_arg, _actuator, setpoint)

    state = api.state_manager.new_state()
    try:
        api.runtime.callback_begin_zone_timestep_after_init_heat_balance(
            state, _on_begin_zone_timestep
        )

        # Run with CWD = sim_dir. EnergyPlus's --readvars post-processor (ReadVarsESO)
        # writes a shared-name scratch file `readvars.audit` in the process working
        # directory; concurrent runs sharing a CWD collide on it ("file already in use
        # by another process" → Severe → exit 1). Per-run CWD isolates it. All path
        # args are absolute so chdir cannot break their resolution.
        args = [
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
                    exit_code = api.runtime.run_energyplus(state, args)
            else:
                exit_code = api.runtime.run_energyplus(state, args)
        finally:
            os.chdir(_prev_cwd)
    finally:
        # Always release the EnergyPlus state, even on error — otherwise C-level
        # resources leak across the many tasks handled by a pool worker.
        api.state_manager.delete_state(state)

    if exit_code != 0:
        log.warning(f"  Simulation {candidate_id} failed (exit {exit_code})")
        return None

    return _extract_hvac_energy(sim_dir)


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

    log.info(f"  ✅ Annual HVAC Energy: {energy} kWh")
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


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(
        description="EnergyPlus GCP VM Standalone Runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--mode",
        choices=["validate", "single", "gsa", "pareto", "exhaustive", "calibrate"],
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
        help="LHS draws for the calibration run (default: 80; dedupes to fewer sims)",
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

    args = parser.parse_args()
    if args.workers < 1:
        args.workers = 1

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
        "pareto": mode_pareto,
        "exhaustive": mode_exhaustive,
        "calibrate": mode_calibrate,
    }
    return dispatch[args.mode](args, idf_local, epw_local)


if __name__ == "__main__":
    sys.exit(main())
