# Decoupling Building Energy Models for Lexicographic Multi-Objective Optimization

Reproducibility companion for *Decoupling Building Energy Models for Lexicographic Multi-Objective Optimization: A Global Sensitivity-Guided Sequential Block-Propagation Framework* (Petreche & Correa, under review at *Energy and Buildings*). The repository ships the pipeline that produced the published results: Sobol-Saltelli variance decomposition over a four-parameter design space, Axiomatic Design block partitioning, and sequential block-propagation against an exhaustive EnergyPlus 5ZoneAirCooled baseline — all driven by a single CLI.

[![Python](https://img.shields.io/badge/Python-3.10+-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/)
[![EnergyPlus](https://img.shields.io/badge/EnergyPlus-v25.1-orange.svg)](https://energyplus.net/)
[![Google Cloud](https://img.shields.io/badge/Cloud-Compute_Engine-4285F4.svg?logo=googlecloud&logoColor=white)](https://cloud.google.com)
[![OR-Tools](https://img.shields.io/badge/Google-OR--Tools_CP--SAT-34A853.svg?logo=google&logoColor=white)](https://developers.google.com/optimization)

---

## Quick Start — Reproduce the Paper Results

The canonical environment is a Google Cloud Compute Engine VM running Ubuntu 22.04 with EnergyPlus 25.1. The full pipeline below was used to generate the Sobol indices, exhaustive baseline, and sequential Pareto front reported in the paper.

**Prerequisites:** Google Cloud SDK on your machine; a GCP project with billing enabled.

```powershell
# 1. Provision and configure the VM (one-time)
gcloud compute instances create sim-test-vm `
  --zone=us-central1-a `
  --image-family=ubuntu-2204-lts --image-project=ubuntu-os-cloud `
  --machine-type=e2-standard-8 --boot-disk-size=30GB

gcloud compute instances stop sim-test-vm --zone=us-central1-a
gcloud compute instances set-service-account sim-test-vm `
  --zone=us-central1-a `
  --service-account=YOUR-PROJECT-NUMBER-compute@developer.gserviceaccount.com `
  --scopes=cloud-platform
gcloud compute instances start sim-test-vm --zone=us-central1-a
gcloud compute config-ssh
```

```bash
# 2. SSH into the VM, clone, and set up the environment (one-time)
git clone https://github.com/joao-petreche-usp/EnergyPlus-API-Colab.git
cd ~/EnergyPlus-API-Colab
bash _GCP_VM_VERSION/config/setup_gcp_env.sh
source _GCP_VM_VERSION/.venv/bin/activate

# 3. Reproduce the paper pipeline
python _GCP_VM_VERSION/run_simulation.py --mode validate              # ~14 s — sanity check
python _GCP_VM_VERSION/run_simulation.py --mode gsa --quiet           # 768 sims — Sobol-Saltelli (N=128)
python _GCP_VM_VERSION/run_simulation.py --mode exhaustive --quiet    # 192 sims — ground truth
python _GCP_VM_VERSION/run_simulation.py --mode pareto-sequential --quiet  # 64 sims — Pareto front
```

Stop the VM when done:

```powershell
gcloud compute instances stop sim-test-vm --zone=us-central1-a
```

Full deployment notes: [`_GCP_VM_VERSION/docs/Deployment_Guide.md`](_GCP_VM_VERSION/docs/Deployment_Guide.md).

---

## Expected Results

Running the pipeline above on the bundled IDF (`5ZoneAirCooled_Opt.idf`) and the Chicago O'Hare TMY3 weather file should reproduce the following tables, which are the ground-truth artifacts a reviewer can compare against.

### Global Sensitivity Analysis (Sobol-Saltelli, N = 128, 768 simulations)

Sobol indices are estimated by the Saltelli (2002) sampler with `N = 128` base samples, producing `N(2k+2) = 768` EnergyPlus simulations for the four-parameter design space (`orient`, `setpoint`, `wall_R`, `roof_R`). Each simulation extracts two outputs: FR₁ (thermal discomfort, °C·h) and FR₂ (annual HVAC electricity, kWh). The interaction terms `|S_T − S₁|` lie within bootstrap noise for every parameter, confirming the near-additive variance structure that licenses lossless sequential block-propagation.

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

The complementary dominance pattern — `setpoint` controls FR₁ (S_T = 0.977) while envelope parameters jointly account for 93 % of FR₂ variance (S_T = 0.536 + 0.398) — is the empirical condition for Suh's Independence Axiom in the discrete variance-based formulation. It directly licenses the two-block partition used by the sequential algorithm. Raw JSON outputs are archived under `_GCP_VM_VERSION/data/`.

### Sequential Block-Propagation Pareto Front (64 simulations, 100 % effectiveness)

Four non-dominated solutions recovered by sequential block-propagation (64 sims = 33 % of the exhaustive 192-cell baseline). All four points coincide exactly with the exhaustive ground truth — **100 % Pareto effectiveness, hypervolume ratio = 1.000, IGD = 0.0**.

| Design | Setpoint (°C) | Orientation | wall_R (m²K/W) | roof_R (m²K/W) | FR₁ — Thermal discomfort (°C·h) | FR₂ — HVAC total energy (kWh/yr) |
|--------|--------------:|:-----------:|---------------:|---------------:|--------------------------------:|---------------------------------:|
| P1 | 23 | North | 2.5 | 4.0 | 2,871 | 62,140 |
| P2 | 24 | North | 2.5 | 4.0 | 5,870 | 61,730 |
| P3 | 25 | North | 2.5 | 4.0 | 8,877 | 61,260 |
| P4 | 26 | North | 2.5 | 4.0 | 11,886 | 60,890 |

Pareto trade-off: a 9,015 °C·h range in cumulative discomfort against 1.25 MWh/yr in HVAC energy over the four setpoint levels. Maximum envelope insulation (`wall_R = 2.5`, `roof_R = 4.0`) and North orientation dominate all four solutions — envelope optimisation is structurally independent of the comfort/setpoint trade-off, directly validating the Axiomatic Design block decomposition. Canonical results in [`_GCP_VM_VERSION/data/reference_results.json`](_GCP_VM_VERSION/data/reference_results.json).

---

## Alternative: Try in Browser (Colab + VS Code)

For readers without a GCP project who want to inspect the API integration without running the full pipeline, the hybrid Colab + VS Code notebook executes a single annual simulation on a Colab runtime with local editing in VS Code.

**Prerequisites:** VS Code with the [Google Colab extension](https://marketplace.visualstudio.com/items?itemName=google.colab); a Google account previously logged in at [colab.research.google.com](https://colab.research.google.com).

1. Open [`_COLAB_VS_CODE_VERSION/EnergyPlus_VS_Code_Colab.ipynb`](_COLAB_VS_CODE_VERSION/EnergyPlus_VS_Code_Colab.ipynb) in VS Code.
2. Connect to a **Colab Runtime** (do not use a local Python kernel).
3. Run cells 1 → 8.

This path does *not* reproduce the Pareto front — it only demonstrates the EnergyPlus Python API integration end-to-end against the shared GCS bucket.

---

## Current Structure

```text
├── _GCP_VM_VERSION/                  # ★ Canonical pipeline (paper reproduction)
│   ├── run_simulation.py             # ★ CLI: validate / single / gsa / pareto / pareto-sequential / exhaustive / calibrate
│   ├── config/
│   │   ├── setup_gcp_env.sh          # ★ One-shot VM bootstrap
│   │   └── requirements/             # Layered deps: base / research / billing / design / dev
│   ├── data/
│   │   ├── 5ZoneAirCooled_Opt.idf                        # Patched IDF model
│   │   ├── USA_IL_Chicago-OHare.Intl.AP.725300_TMY3.epw  # Chicago TMY3 weather
│   │   ├── sobol_saltelli.json       # ★ Sobol-Saltelli GSA (768 sims, 4 DPs)
│   │   └── reference_results.json    # ★ Canonical Pareto front (4 designs, 64 sims)
│   ├── docs/                         # Deployment + contributor guides
│   ├── scripts/                      # build_proxies.py, dashboard.py, reprocess_gsa.py
│   ├── src/                          # Shared library (config + IDF patcher)
│   ├── test/
│   ├── Designer_Decision_Explorer.ipynb      # Post-result analysis notebook
│   └── VERSION_INFO.md
├── _COLAB_VS_CODE_VERSION/           # Browser-friendly demo (single simulation)
│   ├── EnergyPlus_VS_Code_Colab.ipynb
│   └── VERSION_INFO.md
├── LICENSE
└── README.md
```

> **Runtime outputs** (`output/`, `pareto_candidates.csv`, `criteria.csv`, `/tmp/energyplus_sim/`) and local files (`.venv/`, IDE settings) are excluded from version control via `.gitignore`. The shared GCS bucket `eplus-colab-cloud-data` holds inputs under `models/` and `weather/` and receives results under `results/`.

---

## Method in One Paragraph

The framework couples Axiomatic Design with sensitivity-guided sequential block-propagation. A Sobol-Saltelli variance decomposition (`N = 128`, 768 EnergyPlus simulations) quantifies how each design parameter drives the functional requirements; the resulting total-order indices `S_T` are mapped into an approximately triangular Axiomatic Design matrix that defines the block partition: Block 1 — comfort (`orient × setpoint`); Block 2 — envelope (`wall_R × roof_R`). Each block is solved as a discrete optimisation against an annual EnergyPlus simulation — geometry is patched into the IDF, while HVAC setpoints are injected at runtime through the Exchange API. Sequential block-propagation recovers 100 % of the exhaustive Pareto front (4 non-dominated designs, HV ratio = 1.000, IGD = 0.0) at 33 % of the evaluation budget (64 vs. 192 simulations). The near-additive variance structure (interaction terms `|S_T − S₁|` within bootstrap noise) is the empirical condition under which the block decomposition is provably lossless — formal derivation and validation against the exhaustive ground truth are in the paper.

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
