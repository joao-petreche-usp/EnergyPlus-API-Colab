# Decoupling Building Energy Models for Lexicographic Multi-Objective Optimization

Reproducibility companion for *Decoupling Building Energy Models for Lexicographic Multi-Objective Optimization: A Global Sensitivity-Guided Sequential Block-Propagation Framework* (Petreche & Correa, under review at *Energy and Buildings*). The repository ships the pipeline that produced the published results: Sobol-Saltelli variance decomposition over a four-parameter design space, Axiomatic Design block partitioning, and sequential block-propagation against an exhaustive EnergyPlus 5ZoneAirCooled baseline — all driven by a single CLI.

[![Python](https://img.shields.io/badge/Python-3.10+-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/)
[![EnergyPlus](https://img.shields.io/badge/EnergyPlus-v25.1-orange.svg)](https://energyplus.net/)
[![Google Cloud](https://img.shields.io/badge/Cloud-Compute_Engine-4285F4.svg?logo=googlecloud&logoColor=white)](https://cloud.google.com)
[![OR-Tools](https://img.shields.io/badge/Google-OR--Tools_CP--SAT-34A853.svg?logo=google&logoColor=white)](https://developers.google.com/optimization)

---

## Quick Start (local, no cloud account required)

The fastest reviewer path: a laptop with Python 3.10+ and EnergyPlus 25.1.0 installed. The `--no-gcs` flag bypasses Google Cloud Storage entirely and uses the IDF, weather file, and ground-truth Pareto reference that ship in `_GCP_VM_VERSION/data/`.

**Prerequisites:**
- Python 3.10+
- [EnergyPlus 25.1.0](https://energyplus.net/) installed at one of: `/usr/local/EnergyPlus-25-1-0`, `/eplus`, `~/eplus`, or `C:\EnergyPlusV25-1-0`. Override with `EPLUS_DIR=...`.

```bash
git clone https://github.com/joao-petreche-usp/EnergyPlus-API-Colab.git
cd EnergyPlus-API-Colab
python -m venv .venv && source .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1
pip install -r _GCP_VM_VERSION/config/requirements/research.txt

# 1. Sanity check (1 EnergyPlus run, ~10 s)
python -u _GCP_VM_VERSION/run_simulation.py --mode validate --no-gcs

# 2. Reproduce Section 4 Pareto front + M3.9 benchmark (64 sims, ~1.5 min on 8 cores)
python -u _GCP_VM_VERSION/run_simulation.py --mode pareto-sequential --workers 8 --no-gcs
```

**Expected output (deterministic):**

| Step | What you should see |
|---|---|
| `validate` | `Annual HVAC Energy: 64519.5 kWh (Exact match within tolerance)` |
| `pareto-sequential` | `Block 2 valid: 48/48 \| \|P2\| = 4`<br>`Effectiveness : 100.0%` (target ≥ 95%)<br>`HV ratio      : 1.0001` (target ≥ 0.90)<br>`Sims used     : 64 / 192  (66.7% reduction)` |

These two commands reproduce the headline numbers of Section 4 — 100% Pareto effectiveness, HV ratio = 1.000, and 66.7% computational savings against the 192-sim full-factorial baseline — without invoking any cloud service.

---

## Reproducing each table and figure

Every paper artefact can be re-derived from the shipped data and code. Pick the mode that matches the section you want to verify.

| Paper section | Mode | EnergyPlus sims | Time (e2-standard-8 / 8 cores) | Output JSON |
|---|---|---|---|---|
| §3 sanity check (single design) | `validate` | 1 | ~10 s | `eplusout.err` + log |
| §3.5 Sobol-Saltelli (Tables 1 & 2) | `gsa-sobol` | 768 (N = 128 × (k + 2)) | ~3 h sequential / ~25 min on 16 cores | `gsa_results_v2.json`, `gsa_results_v2.csv`, `figure1_sobol_heatmap_v2.png` |
| §4.1 full-factorial ground truth | `exhaustive` | 192 (4 × 4 × 3 × 4) | ~40 min on 8 cores | `exhaustive_pareto_ref.json` |
| §4.2 sequential block-propagation Pareto front | `pareto-sequential` | 64 (16 block-1 + 48 block-2) | ~1.5 min on 8 cores | `pareto_sequential_<timestamp>.json` |
| §4.2 CP-SAT marginal calibration | `calibrate` | (consumes existing GSA samples) | < 1 min | `proxy_coefficients.json`, `marginal_tables.json` |

All Section-4 numbers are reachable with `validate` + `pareto-sequential` alone — both ship a pre-computed ground truth (`_GCP_VM_VERSION/data/exhaustive_pareto_ref.json`, 57 KB), so the M3.9 benchmark (`Effectiveness`, `HV ratio`) prints inline without re-running the 192-sim baseline.

To regenerate the ground truth from scratch, run `--mode exhaustive` first (1 h on 8 cores).

---

## Reviewer reproducibility — empirical proxy & discretisation metrics (Section 5.2)

Three pre-submission validation artefacts back the claims in manuscript Section 5.2 — proxy calibration (Reviewer-2 Concern 1), AD-matrix threshold stability (Concern 2), and Saltelli continuous-vs-discrete bias (Concern 3). All three are regenerable from the cached 768-sample GSA dataset (`_GCP_VM_VERSION/data/gsa_results_v2.csv`) without any EnergyPlus runs:

```bash
python -u _GCP_VM_VERSION/scripts/reproduce_reviewer_metrics.py
```

Runtime: under one minute on a laptop. Output:

- `_GCP_VM_VERSION/data/proxy_calibration_p1.json` — R²(`fr1_proxy`, `fr1_kh`) = 0.913, Spearman ρ = +0.942, Kendall τ = 0.808.
- `_GCP_VM_VERSION/data/discretization_sensitivity.json` — max |ΔS_T| = 0.57% on partition-relevant DPs; ranking preserved.
- `_GCP_VM_VERSION/data/ad_matrix_threshold_sweep.json` — partition unchanged under threshold sweep {0.05, 0.10, 0.15}.
- `figures/fig_proxy_calibration.{pdf,png}`, `figures/fig_discretization_bar.{pdf,png}`.

The S_T-based AD matrix construction is canonical in `_GCP_VM_VERSION/src/analysis/ad_matrix.py:build_ad_matrix(..., use_st=True)`.

---

## Alternative reviewer journeys

### Google Cloud VM (for the large GSA campaign)

For reviewers comfortable with GCP who want to re-run the full Sobol-Saltelli campaign (`--mode gsa-sobol`, 768 sims, ~25 min on 16 cores), see [`_GCP_VM_VERSION/docs/Deployment_Guide.md`](_GCP_VM_VERSION/docs/Deployment_Guide.md) §3.

### Browser (Colab + VS Code)

A single-simulation demo that does *not* reproduce the Pareto front but shows the EnergyPlus Python API integration end-to-end:

1. Open [`_COLAB_VS_CODE_VERSION/EnergyPlus_VS_Code_Colab.ipynb`](_COLAB_VS_CODE_VERSION/EnergyPlus_VS_Code_Colab.ipynb) in VS Code.
2. Connect to a Colab Runtime (do not use a local Python kernel).
3. Run cells 1 → 8.

Prerequisite: VS Code with the [Google Colab extension](https://marketplace.visualstudio.com/items?itemName=google.colab) and a Google account previously logged in at <https://colab.research.google.com>.

---

## Pre-computed result tables

### Sobol indices (Section 3.5, `--mode gsa-sobol`)

Sobol indices estimated by the Saltelli (2002) sampler with `N = 128` base samples, producing `N(k+2) = 768` EnergyPlus simulations for the four-parameter design space (`orient`, `setpoint`, `wall_R`, `roof_R`). The interaction terms `|S_T − S₁|` lie within bootstrap noise for every parameter, confirming the near-additive variance structure that licenses lossless sequential block-propagation.

**Table 1 — Sobol indices for FR₁ (thermal discomfort, °C·h).**

| Rank | Design Parameter | S₁ | S_T | 95% CI half-width (S_T) | AD block |
|------|-----------------|----:|----:|----:|----------|
| 1 | `setpoint` | 0.985 | 0.977 | ±0.168 | Block 1 — comfort |
| 2 | `orient` | 0.025 | 0.015 | ±0.004 | Block 1 — comfort |
| 3 | `wall_R` | 0.011 | 0.010 | ±0.003 | Block 2 — envelope |
| 4 | `roof_R` | 0.008 | 0.007 | ±0.002 | Block 2 — envelope |

**Table 2 — Sobol indices for FR₂ (annual HVAC energy, kWh/yr).**

| Rank | Design Parameter | S₁ | S_T | 95% CI half-width (S_T) | AD block |
|------|-----------------|----:|----:|----:|----------|
| 1 | `roof_R` | 0.548 | 0.536 | ±0.118 | Block 2 — envelope |
| 2 | `wall_R` | 0.398 | 0.398 | ±0.112 | Block 2 — envelope |
| 3 | `setpoint` | 0.040 | 0.044 | ±0.013 | Block 1 — comfort |
| 4 | `orient` | 0.015 | 0.014 | ±0.004 | Block 1 — comfort |

Raw outputs: `_GCP_VM_VERSION/data/gsa_results_v2.{json,csv}`, `_GCP_VM_VERSION/data/sobol_indices_n128.json`.

### Sequential block-propagation Pareto front (Section 4.2, `--mode pareto-sequential`)

Four non-dominated solutions recovered by sequential block-propagation (64 sims = 33% of the exhaustive 192-cell baseline). All four points coincide exactly with the exhaustive ground truth — **100% Pareto effectiveness, HV ratio = 1.000, IGD = 0.0**.

| Design | Setpoint (°C) | Orientation | wall_R (m²K/W) | roof_R (m²K/W) | FR₁ — Thermal discomfort (°C·h) | FR₂ — HVAC total energy (kWh/yr) |
|--------|--------------:|:-----------:|---------------:|---------------:|--------------------------------:|---------------------------------:|
| P1 | 23 | North | 2.5 | 4.0 | 2,871 | 62,140 |
| P2 | 24 | North | 2.5 | 4.0 | 5,870 | 61,730 |
| P3 | 25 | North | 2.5 | 4.0 | 8,877 | 61,260 |
| P4 | 26 | North | 2.5 | 4.0 | 11,886 | 60,890 |

Pareto trade-off: 9,015 °C·h range in cumulative discomfort against 1.25 MWh/yr in HVAC energy over the four setpoint levels. Maximum envelope insulation (`wall_R = 2.5`, `roof_R = 4.0`) and North orientation dominate all four solutions — envelope optimisation is structurally independent of the comfort/setpoint trade-off, directly validating the Axiomatic Design block decomposition. Canonical results in [`_GCP_VM_VERSION/data/reference_results.json`](_GCP_VM_VERSION/data/reference_results.json) and `_GCP_VM_VERSION/data/exhaustive_pareto_ref.json`.

---

## Repository structure

```text
├── _GCP_VM_VERSION/                       # ★ Canonical pipeline (paper reproduction)
│   ├── run_simulation.py                  # ★ CLI: validate / single / gsa / gsa-sobol /
│   │                                      #         exhaustive / pareto / pareto-sequential / calibrate
│   ├── config/
│   │   ├── setup_gcp_env.sh               # One-shot GCP VM bootstrap
│   │   └── requirements/                  # Layered deps: base / research / billing / design / dev
│   ├── data/
│   │   ├── 5ZoneAirCooled_Opt.idf                         # Patched IDF model
│   │   ├── USA_IL_Chicago-OHare.Intl.AP.725300_TMY3.epw   # Chicago TMY3 weather
│   │   ├── reference_results.json         # ★ Canonical single-zone HVAC ground truth (64519.5 kWh)
│   │   ├── exhaustive_pareto_ref.json     # ★ 192-sim ground-truth Pareto front (Section 4)
│   │   ├── gsa_results_v2.{json,csv}      # ★ Sobol-Saltelli GSA (768 sims, 4 DPs)
│   │   ├── sobol_indices_n128.json        # ★ Pre-computed Sobol indices (Tables 1, 2)
│   │   ├── gsa_ad_matrix_v2.json          # Axiomatic Design matrix (S_T values)
│   │   ├── marginal_tables.json           # CP-SAT marginal calibration
│   │   ├── proxy_coefficients.json        # CP-SAT regression calibration
│   │   └── pareto_front.json              # Block-1 sequential output
│   ├── docs/                              # Deployment + contributor guides
│   ├── scripts/                           # build_proxies.py, dashboard.py, reprocess_gsa.py
│   ├── src/                               # Shared library (config + IDF patcher + analysis)
│   ├── test/
│   ├── Designer_Decision_Explorer.ipynb   # Post-result analysis notebook
│   └── VERSION_INFO.md
├── _COLAB_VS_CODE_VERSION/                # Browser-friendly demo (single simulation)
│   ├── EnergyPlus_VS_Code_Colab.ipynb
│   └── VERSION_INFO.md
├── LICENSE
└── README.md
```

Runtime outputs (`output/`, `pareto_candidates.csv`, `criteria.csv`, `/tmp/energyplus_sim/`) and local files (`.venv/`, IDE settings) are excluded from version control via `.gitignore`. The shared GCS bucket `eplus-colab-cloud-data` (cloud-only path) holds inputs under `models/` and `weather/` and receives results under `results/`.

---

## Method in one paragraph

The framework couples Axiomatic Design with sensitivity-guided sequential block-propagation. A Sobol-Saltelli variance decomposition (`N = 128`, 768 EnergyPlus simulations) quantifies how each design parameter drives the functional requirements; the resulting total-order indices `S_T` are mapped into an approximately triangular Axiomatic Design matrix that defines the block partition: Block 1 — comfort (`orient × setpoint`); Block 2 — envelope (`wall_R × roof_R`). Each block is solved as a discrete optimisation against an annual EnergyPlus simulation — geometry is patched into the IDF, while HVAC setpoints are injected at runtime through the Exchange API. Sequential block-propagation recovers 100% of the exhaustive Pareto front (4 non-dominated designs, HV ratio = 1.000, IGD = 0.0) at 33% of the evaluation budget (64 vs. 192 simulations). The near-additive variance structure (interaction terms `|S_T − S₁|` within bootstrap noise) is the empirical condition under which the block decomposition is provably lossless — formal derivation and validation against the exhaustive ground truth are in the paper.

---

## Citation

```bibtex
@article{petreche_decoupling_2026,
  author  = {Petreche, Jo{\~a}o Roberto Diego and Correa, Fabiano},
  title   = {Decoupling Building Energy Models for Lexicographic Multi-Objective
             Optimization: A Global Sensitivity-Guided Sequential
             Block-Propagation Framework},
  journal = {Energy and Buildings (under review)},
  year    = {2026},
  note    = {Companion code: \url{https://github.com/joao-petreche-usp/EnergyPlus-API-Colab}}
}
```

---

## Support

- Issues: <https://github.com/joao-petreche-usp/EnergyPlus-API-Colab/issues>
- Discussions: <https://github.com/joao-petreche-usp/EnergyPlus-API-Colab/discussions>
