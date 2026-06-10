"""
ad_matrix.py — Axiomatic Design triangular matrix from Sobol indices (M2.7).

Reads gsa_results.json (produced by gsa_analysis.analyze_both) and constructs
the AD independence matrix. Two construction modes:

  use_st=True (default, canonical post-Reviewer-2 hardening):
      Partitioning uses total-order indices S_T (matches manuscript §2.2 /
      Theorem 1). For each DP, computes |S_T - S_1| and checks whether the
      95% bootstrap CI half-widths (S1_conf, ST_conf) sum to at least |delta|
      — if so, the interactions are statistically indistinguishable from zero
      and the near-additivity precondition of Theorem 1 holds.

  use_st=False:
      Legacy S_1-based partition (heuristic — pre-PR feat/reviewer-2-hardening).
      Kept for retrocompat in callers that have not migrated.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_FR_LABELS = {"fr1": "FR1 (comfort)", "fr2": "FR2 (energy)"}


def build_ad_matrix(
    sobol_results: dict,
    threshold: float = 0.05,
    use_st: bool = True,
) -> dict:
    """Build the AD independence matrix from Sobol indices.

    For each DP, with index_key = "ST" if use_st else "S1":
      - dominant_fr   : FR with the higher index value
      - s1/st         : raw S1 and S_T per FR
      - delta_fr1     : |S_T(FR1) - S_1(FR1)| (only when use_st)
      - delta_fr2     : |S_T(FR2) - S_1(FR2)|
      - near_additive : True if delta_fr1 <= S1_conf+ST_conf for FR1 AND idem FR2
      - threshold_st_warning : True if any index ∈ (threshold, threshold+0.05)
        — signals Saltelli instability zone (re-check with higher N).
      - coupled       : True if BOTH index values exceed `threshold`
      - decoupled     : True if only one FR exceeds `threshold`
      - inert         : True if neither

    Returns a dict with "threshold", "use_st", "dps" (list of per-DP dicts),
    "markdown_table", and "summary" (counts of coupled/decoupled/inert).
    """
    idx_key = "ST" if use_st else "S1"
    fr1_idx = sobol_results["fr1"][idx_key]
    fr2_idx = sobol_results["fr2"][idx_key]
    names = sobol_results["problem"]["names"]

    # Confidence intervals (95% bootstrap) and the *other* index for delta.
    has_st = "ST" in sobol_results["fr1"] and "ST" in sobol_results["fr2"]
    fr1_s1 = sobol_results["fr1"].get("S1", {})
    fr2_s1 = sobol_results["fr2"].get("S1", {})
    fr1_st = sobol_results["fr1"].get("ST", {})
    fr2_st = sobol_results["fr2"].get("ST", {})
    fr1_s1c = sobol_results["fr1"].get("S1_conf", {})
    fr2_s1c = sobol_results["fr2"].get("S1_conf", {})
    fr1_stc = sobol_results["fr1"].get("ST_conf", {})
    fr2_stc = sobol_results["fr2"].get("ST_conf", {})

    rows = []
    for name in names:
        val_1 = float(fr1_idx[name])
        val_2 = float(fr2_idx[name])
        above_1 = val_1 > threshold
        above_2 = val_2 > threshold
        dominant = "fr1" if val_1 >= val_2 else "fr2"

        row = {
            "dp": name,
            "s1_fr1": round(float(fr1_s1.get(name, 0.0)), 4),
            "s1_fr2": round(float(fr2_s1.get(name, 0.0)), 4),
            "st_fr1": round(float(fr1_st.get(name, 0.0)), 4) if has_st else None,
            "st_fr2": round(float(fr2_st.get(name, 0.0)), 4) if has_st else None,
            "dominant_fr": dominant,
            "coupled": bool(above_1 and above_2),
            "decoupled": bool(above_1 ^ above_2),
            "inert": bool(not above_1 and not above_2),
        }

        if use_st and has_st:
            s1_1, st_1 = float(fr1_s1.get(name, 0.0)), float(fr1_st.get(name, 0.0))
            s1_2, st_2 = float(fr2_s1.get(name, 0.0)), float(fr2_st.get(name, 0.0))
            d1 = abs(st_1 - s1_1)
            d2 = abs(st_2 - s1_2)
            ci_sum_1 = float(fr1_s1c.get(name, 0.0)) + float(fr1_stc.get(name, 0.0))
            ci_sum_2 = float(fr2_s1c.get(name, 0.0)) + float(fr2_stc.get(name, 0.0))
            near_1 = d1 <= ci_sum_1
            near_2 = d2 <= ci_sum_2
            row.update({
                "delta_fr1": round(d1, 4),
                "delta_fr2": round(d2, 4),
                "delta_ci_sum_fr1": round(ci_sum_1, 4),
                "delta_ci_sum_fr2": round(ci_sum_2, 4),
                "near_additive_fr1": bool(near_1),
                "near_additive_fr2": bool(near_2),
                "near_additive": bool(near_1 and near_2),
            })
            row["threshold_st_warning"] = bool(
                (threshold < val_1 <= threshold + 0.05)
                or (threshold < val_2 <= threshold + 0.05)
            )

        rows.append(row)

    table = _markdown_table(rows, threshold, use_st=use_st and has_st)
    summary = {
        "n_coupled": sum(1 for r in rows if r["coupled"]),
        "n_decoupled": sum(1 for r in rows if r["decoupled"]),
        "n_inert": sum(1 for r in rows if r["inert"]),
        "all_near_additive": (
            all(r.get("near_additive", False) for r in rows)
            if use_st and has_st else None
        ),
    }
    result = {
        "threshold": threshold,
        "use_st": use_st and has_st,
        "dps": rows,
        "summary": summary,
        "markdown_table": table,
    }

    _log_summary(rows, use_st=use_st and has_st)
    return result


def threshold_sensitivity_sweep(
    sobol_results: dict,
    thresholds: tuple[float, ...] = (0.05, 0.10, 0.15),
    use_st: bool = True,
) -> dict:
    """Run build_ad_matrix at multiple thresholds; report partition stability.

    Returns dict mapping threshold -> {"coupled": [...], "decoupled": [...],
    "inert": [...]} so the caller can detect classification flips and surface
    them in manuscript §5.1.
    """
    sweep = {}
    for thr in thresholds:
        ad = build_ad_matrix(sobol_results, threshold=thr, use_st=use_st)
        sweep[f"{thr:.2f}"] = {
            "coupled": [r["dp"] for r in ad["dps"] if r["coupled"]],
            "decoupled": [r["dp"] for r in ad["dps"] if r["decoupled"]],
            "inert": [r["dp"] for r in ad["dps"] if r["inert"]],
        }
    return sweep


def _markdown_table(rows: list[dict], threshold: float, use_st: bool) -> str:
    if use_st:
        lines = [
            f"<!-- AD matrix — S_T threshold = {threshold}, near-additive via 95% CI overlap -->",
            "| DP | S1 FR1 | ST FR1 | S1 FR2 | ST FR2 | |ST-S1| FR1 | |ST-S1| FR2 | CI sum FR1 | CI sum FR2 | Near-additive | Dominant | Status |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for r in rows:
            status = "coupled" if r["coupled"] else ("decoupled" if r["decoupled"] else "inert")
            na = "yes" if r.get("near_additive") else "NO"
            lines.append(
                f"| {r['dp']} | {r['s1_fr1']:.4f} | {r['st_fr1']:.4f} | {r['s1_fr2']:.4f} | {r['st_fr2']:.4f} "
                f"| {r.get('delta_fr1', 0):.4f} | {r.get('delta_fr2', 0):.4f} "
                f"| {r.get('delta_ci_sum_fr1', 0):.4f} | {r.get('delta_ci_sum_fr2', 0):.4f} "
                f"| {na} | {r['dominant_fr']} | {status} |"
            )
        return "\n".join(lines)

    lines = [
        f"<!-- AD matrix — S1 threshold = {threshold} (legacy) -->",
        "| DP | S1 FR1 | S1 FR2 | Dominant | Status |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        status = "coupled" if r["coupled"] else ("decoupled" if r["decoupled"] else "inert")
        lines.append(
            f"| {r['dp']} | {r['s1_fr1']:.4f} | {r['s1_fr2']:.4f} "
            f"| {r['dominant_fr']} | {status} |"
        )
    return "\n".join(lines)


def _log_summary(rows: list[dict], use_st: bool) -> None:
    log.info("\n=== AD Independence Matrix ===")
    if use_st:
        log.info(
            f"{'DP':<14} {'S1 F1':>7} {'ST F1':>7} {'S1 F2':>7} {'ST F2':>7} "
            f"{'|d|F1':>6} {'|d|F2':>6}  {'NearAdd':<8} {'Status'}"
        )
        log.info("-" * 90)
        for r in rows:
            status = "COUPLED" if r["coupled"] else ("decoupled" if r["decoupled"] else "inert")
            na = "yes" if r.get("near_additive") else "NO"
            log.info(
                f"{r['dp']:<14} {r['s1_fr1']:>7.4f} {r['st_fr1']:>7.4f} "
                f"{r['s1_fr2']:>7.4f} {r['st_fr2']:>7.4f} "
                f"{r.get('delta_fr1', 0):>6.4f} {r.get('delta_fr2', 0):>6.4f}  "
                f"{na:<8} {status}"
            )
    else:
        log.info(f"{'DP':<14} {'S1 FR1':>8} {'S1 FR2':>8}  {'Dominant':<10} {'Status'}")
        log.info("-" * 56)
        for r in rows:
            status = "COUPLED" if r["coupled"] else ("decoupled" if r["decoupled"] else "inert")
            log.info(
                f"{r['dp']:<14} {r['s1_fr1']:>8.4f} {r['s1_fr2']:>8.4f}  "
                f"{r['dominant_fr']:<10} {status}"
            )


def save(result: dict, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    log.info(f"Saved AD matrix → {path}")


def load(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
