"""
ad_matrix.py — Axiomatic Design triangular matrix from Sobol indices (M2.7)

Reads gsa_results.json (produced by gsa_analysis.analyze_both) and constructs
the AD independence matrix: for each DP, which FR dominates and is the coupling
below the threshold. Outputs a dict and a markdown table ready for the paper.
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
) -> dict:
    """Build the AD independence matrix from Sobol S1 indices.

    For each DP:
      - dominant_fr  : FR with the higher S1
      - s1_fr1/fr2   : raw S1 values
      - coupled      : True if BOTH S1 values exceed `threshold`
      - decoupled    : True if only one FR exceeds `threshold`

    Returns a dict with keys "dps" (list of per-DP dicts) and "markdown_table".
    """
    fr1 = sobol_results["fr1"]["S1"]
    fr2 = sobol_results["fr2"]["S1"]
    names = sobol_results["problem"]["names"]

    rows = []
    for name in names:
        s1_1 = fr1[name]
        s1_2 = fr2[name]
        above_1 = s1_1 > threshold
        above_2 = s1_2 > threshold
        dominant = "fr1" if s1_1 >= s1_2 else "fr2"
        rows.append(
            {
                "dp": name,
                "s1_fr1": round(s1_1, 4),
                "s1_fr2": round(s1_2, 4),
                "dominant_fr": dominant,
                "coupled": bool(above_1 and above_2),
                "decoupled": bool(above_1 ^ above_2),
                "inert": bool(not above_1 and not above_2),
            }
        )

    table = _markdown_table(rows, threshold)
    result = {"threshold": threshold, "dps": rows, "markdown_table": table}

    _log_summary(rows)
    return result


def _markdown_table(rows: list[dict], threshold: float) -> str:
    lines = [
        f"<!-- AD matrix — S1 threshold = {threshold} -->",
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


def _log_summary(rows: list[dict]) -> None:
    log.info("\n=== AD Independence Matrix ===")
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
