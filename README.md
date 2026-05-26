# Hierarchical and Sequential Multi-Objective Optimization of Buildings

Reproducibility companion for the paper *Hierarchical and Sequential Multi-Objective Optimization of Buildings: Implementing Axiomatic Design via Google OR-Tools and EnergyPlus Python API* (Petreche, USP). The repository ships the pipeline that produced the published Pareto front: Global Sensitivity Analysis (Morris) → CP-SAT lexicographic optimization → EnergyPlus simulation, all driven by a single CLI.

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
  --machine-type=e2-medium --boot-disk-size=30GB

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
python _GCP_VM_VERSION/run_simulation.py --mode validate   # ~14 s — sanity check
python _GCP_VM_VERSION/run_simulation.py --mode gsa --n-morris 20   # ~45 min, use tmux
python _GCP_VM_VERSION/run_simulation.py --mode pareto     # produces the published Pareto front
```

Stop the VM when done:

```powershell
gcloud compute instances stop sim-test-vm --zone=us-central1-a
```

Full deployment notes: [`_GCP_VM_VERSION/docs/Deployment_Guide.md`](_GCP_VM_VERSION/docs/Deployment_Guide.md).

---

## Expected Results

Running the pipeline above on the bundled IDF (`5ZoneAirCooled_Opt.idf`) and Chicago TMY3 weather file should reproduce the following tables. They are the ground-truth artifacts a reviewer can compare against.

### Global Sensitivity Analysis (Morris, 5 DPs, 90 simulations)

| Rank | Design Parameter | μ\* (importance) | σ (interactions) | Role in AD hierarchy |
|------|------------------|------------------|------------------|----------------------|
| 1 | `orientation` | 4548.9 | 6017.0 | Level 1 — fix first |
| 2 | `wall_r`      | 2646.1 | 3854.3 | Level 2 |
| 3 | `roof_r`      | 2548.6 | 3570.5 | Level 2 (coupled with `wall_r`) |
| 4 | `setpoint`    | 2274.2 | 3654.7 | Level 3 |
| 5 | `glass_u`     | 1906.4 | 3326.7 | Level 4 |

Raw JSON: [`_GCP_VM_VERSION/data/gsa_results_20260518.json`](_GCP_VM_VERSION/data/gsa_results_20260518.json).

### MLT Pareto Front (two-level lexicographic, 48 simulations)

| Candidate | Setpoint (°C) | Orientation | wall_r | roof_r | FR1 proxy | FR2 — HVAC Energy (kWh/yr) |
|---|---|---|---|---|---|---|
| MLT\_o0\_s23\_b65 | 23 | 0° (North) | 2.5 | 4.0 | 17.072 | 62,136 |
| MLT\_o0\_s24\_b65 | 24 | 0° (North) | 2.5 | 4.0 | 17.616 | 61,728 |
| MLT\_o0\_s25\_b65 | 25 | 0° (North) | 2.5 | 4.0 | 18.160 | 61,258 |

Budget range b30→b65: 67,694 → 62,136 kWh/yr (8.2% envelope-driven variation). Raw JSON: [`_GCP_VM_VERSION/data/pareto_front.json`](_GCP_VM_VERSION/data/pareto_front.json).

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
│   ├── data/                         # IDF, EPW, GSA results, Pareto front, proxy coefficients
│   ├── docs/                         # Deployment + contributor guides
│   ├── scripts/                      # build_proxies.py, dashboard.py, reprocess_gsa.py, billing utils
│   ├── src/                          # Shared library (config + IDF patcher)
│   ├── test/
│   ├── Designer_Decision_Explorer.ipynb      # Post-result analysis notebook
│   ├── EnergyPlus_API_GCP_VM.ipynb           # Reference notebook (legacy)
│   └── VERSION_INFO.md
├── _COLAB_VS_CODE_VERSION/           # Browser-friendly demo (single simulation)
│   ├── EnergyPlus_VS_Code_Colab.ipynb
│   └── VERSION_INFO.md
├── local-docs/                       # Local-only reference material (not tracked in git)
│   ├── docs-claude/                  # Theory docs loaded as AI context
│   ├── docs-zotero/                  # PDF library (key references)
│   ├── docs-google/                  # GCP proposals, presentations, cost estimates
│   └── docs-github/                  # GitHub tokens (local only)
├── LICENSE
└── README.md
```

> **Runtime outputs** (`output/`, `pareto_candidates.csv`, `criteria.csv`, `/tmp/energyplus_sim/`) and local files (`local-docs/`, `.venv/`, IDE settings) are excluded from version control via `.gitignore`. The shared GCS bucket `eplus-colab-cloud-data` (configurable) holds inputs under `models/` and `weather/` and receives results under `resultados/`.

---

## Method in One Paragraph

The framework couples Axiomatic Design with Google OR-Tools CP-SAT. A Morris-method Global Sensitivity Analysis quantifies how the design parameters drive the functional requirements; the resulting μ\* ranking is mapped into a triangular AD matrix that defines the lexicographic optimization hierarchy. CP-SAT solves the discrete two-level model (envelope budget → setpoint), and each candidate is verified by an annual EnergyPlus simulation — building geometry is patched into the IDF, while HVAC setpoints are injected at runtime through the Exchange API. The full derivation, validation against an exhaustive ground truth, and discussion of the linearization variants are in the paper.

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
