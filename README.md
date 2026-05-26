# Hierarchical and Sequential Multi-Objective Optimization of Buildings

Reproducibility companion for the paper *Hierarchical and Sequential Multi-Objective Optimization of Buildings: Implementing Axiomatic Design via Google OR-Tools and EnergyPlus Python API* (Petreche, USP). The repository ships the pipeline that produced the published Pareto front: Sobol variance decomposition (192-simulation full factorial) → CP-SAT lexicographic optimization → EnergyPlus simulation, all driven by a single CLI.

[![Python](https://img.shields.io/badge/Python-3.10+-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/)
[![EnergyPlus](https://img.shields.io/badge/EnergyPlus-v25.1-orange.svg)](https://energyplus.net/)
[![Google Cloud](https://img.shields.io/badge/Cloud-Compute_Engine-4285F4.svg?logo=googlecloud&logoColor=white)](https://cloud.google.com)
[![OR-Tools](https://img.shields.io/badge/Google-OR--Tools_CP--SAT-34A853.svg?logo=google&logoColor=white)](https://developers.google.com/optimization)

---

## Quick Start — Reproduce the Paper Results

The canonical environment is a Google Cloud Compute Engine VM running Ubuntu 22.04 with EnergyPlus 25.1. The full pipeline below was used to generate the figures and tables in the paper.

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

Running the pipeline above on the bundled IDF (`5ZoneAirCooled_Opt.idf`) and Chicago TMY3 weather file should reproduce the following tables. They are the ground-truth artifacts a reviewer can compare against.

### Global Sensitivity Analysis (Sobol exact, 4 DPs, 192 simulations)

Sobol total-order indices computed via ANOVA over the complete 4-DP full factorial (orient × setpoint × wall_R × roof_R, 192 simulations). The near-zero interaction terms (S_T − S₁ < 0.003) confirm the near-additive variance structure that validates exact Pareto recovery under block-propagation.

| Rank | Design Parameter | S_T (total-order) | S₁ (first-order) | S_T − S₁ | AD block |
|------|-----------------|-------------------|-------------------|-----------|----------|
| 1 | `roof_R` | 0.5032 | 0.5009 | 0.0023 | Block 2 — envelope |
| 2 | `wall_R` | 0.4589 | 0.4568 | 0.0021 | Block 2 — envelope |
| 3 | `setpoint` | 0.0317 | 0.0311 | 0.0006 | Block 1 — comfort |
| 4 | `orient` | 0.0090 | 0.0084 | 0.0006 | Block 1 — comfort |

Raw JSON: [`_GCP_VM_VERSION/data/sobol_exact.json`](_GCP_VM_VERSION/data/sobol_exact.json).

### Sequential Block-Propagation Pareto Front (64 simulations, 100% effectiveness)

Four non-dominated solutions recovered by sequential block-propagation (64 sims = 33% of exhaustive 192). All four match the exhaustive ground truth exactly — **100% Pareto effectiveness**, 0 solutions missed.

| Design | Setpoint (°C) | Orientation | wall_R (m²K/W) | roof_R (m²K/W) | FR1 proxy | FR2 — Total Energy (kWh/yr) |
|--------|--------------|-------------|----------------|----------------|-----------|------------------------------|
| P1 | 23 | North | 2.5 | 4.0 | 21.63 | 62,136 |
| P2 | 24 | North | 2.5 | 4.0 | 22.18 | 61,728 |
| P3 | 25 | North | 2.5 | 4.0 | 22.72 | 61,258 |
| P4 | 26 | North | 2.5 | 4.0 | 23.26 | 60,886 |

Pareto trade-off: 1.63 °C comfort vs. 1.25 MWh/yr energy over four setpoint levels. Maximum insulation (wall_R = 2.5, roof_R = 4.0) and North orientation dominate all four solutions — envelope optimisation is independent of the comfort/setpoint trade-off, validating the Axiomatic Design block decomposition. Canonical results in [`_GCP_VM_VERSION/data/reference_results.json`](_GCP_VM_VERSION/data/reference_results.json).

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
│   ├── run_simulation.py             # ★ CLI: validate / single / gsa / pareto / exhaustive / calibrate
│   ├── config/
│   │   ├── setup_gcp_env.sh          # ★ One-shot VM bootstrap
│   │   └── requirements/             # Layered deps: base / research / billing / design / dev
│   ├── data/
│   │   ├── 5ZoneAirCooled_Opt.idf                        # Patched IDF model
│   │   ├── USA_IL_Chicago-OHare.Intl.AP.725300_TMY3.epw  # Chicago TMY3 weather
│   │   ├── sobol_exact.json          # ★ Sobol exact GSA (192 sims, 4 DPs)
│   │   ├── reference_results.json    # ★ Canonical Pareto front (4 designs, 64 sims)
│   │   ├── proxy_coefficients.json   # CP-SAT Variant A — regression coefficients
│   │   └── marginal_tables.json      # CP-SAT Variant B — marginal contribution tables
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

The framework couples Axiomatic Design with Google OR-Tools CP-SAT. A Sobol variance decomposition — computed exactly via ANOVA over a 192-simulation full factorial — quantifies how the design parameters drive the functional requirements; the resulting S_T ranking is mapped into a triangular AD matrix that defines the lexicographic optimization hierarchy and block decomposition. CP-SAT solves the discrete two-block model (Block 1: orientation × setpoint; Block 2: wall insulation × roof insulation), and each candidate is verified by an annual EnergyPlus simulation — building geometry is patched into the IDF, while HVAC setpoints are injected at runtime through the Exchange API. Sequential block-propagation recovers 100% of the exhaustive Pareto front at 33% of the evaluation budget (64 vs. 192 simulations). The full derivation, validation against the exhaustive ground truth, and discussion of the CP-SAT linearization variants are in the paper.

---

## Citation

```bibtex
@article{petreche_hsmoo_2026,
  author  = {Petreche, Jo{\~a}o Roberto Diego},
  title   = {Hierarchical and Sequential Multi-Objective Optimization of Buildings:
             Implementing Axiomatic Design via Google OR-Tools and EnergyPlus Python API},
  journal = {(in preparation)},
  year    = {2026},
  note    = {Companion code: \url{https://github.com/joao-petreche-usp/EnergyPlus-API-Colab}}
}
```

---

## Support

- Issues: <https://github.com/joao-petreche-usp/EnergyPlus-API-Colab/issues>
- Discussions: <https://github.com/joao-petreche-usp/EnergyPlus-API-Colab/discussions>
