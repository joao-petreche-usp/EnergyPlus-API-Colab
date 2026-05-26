# Deployment Guide — `run_simulation.py` on GCP VM

Operational reference for running the EnergyPlus + CP-SAT pipeline on a Google Cloud Compute Engine VM. The high-level Quick Start lives in the [project README](../../README.md); this document covers invocation patterns, execution modes, the local `--no-gcs` workflow, Stage-Out paths, and the layered dependency files.

---

## 1. Invocation Patterns

PYTHONPATH is resolved internally — the script adds `_GCP_VM_VERSION/` to `sys.path` automatically, so it runs from any directory:

```bash
# From the repository root
cd ~/EnergyPlus-API-Colab
python _GCP_VM_VERSION/run_simulation.py --mode validate

# From inside _GCP_VM_VERSION/
cd ~/EnergyPlus-API-Colab/_GCP_VM_VERSION
python run_simulation.py --mode validate

# Via an absolute path
python ~/EnergyPlus-API-Colab/_GCP_VM_VERSION/run_simulation.py --mode validate
```

---

## 2. Deployment Flow on the GCP VM

### Prerequisites
- VM with Ubuntu 22.04, service account attached (`cloud-platform` scope)
- EnergyPlus 25.1.0 installed at `/usr/local/EnergyPlus-25-1-0` (handled by `setup_gcp_env.sh`)
- Repository cloned at `~/EnergyPlus-API-Colab`

### Step by Step

**1. Update the repository:**
```bash
cd ~/EnergyPlus-API-Colab
git pull origin main
```

**2. Activate the venv (created by `setup_gcp_env.sh`):**
```bash
source _GCP_VM_VERSION/.venv/bin/activate
```

**3. Quick validation (~12s, single simulation):**
```bash
python _GCP_VM_VERSION/run_simulation.py --mode validate
```

Expected tail of the output:
```
✅ Annual HVAC Energy: XXXX.XX kWh
✅ Validation OK — ready to scale (gsa or pareto mode).
```

**4. Test GSA with a small N (~25 min):**
```bash
python _GCP_VM_VERSION/run_simulation.py --mode gsa --n-morris 20
```

**5. Full Pareto pipeline:**
```bash
python _GCP_VM_VERSION/run_simulation.py --mode pareto
```

---

## 3. Local-Only Usage (No GCS)

For local development without uploading to the GCS bucket, pass `--no-gcs`:

```powershell
# Windows
.\.venv\Scripts\Activate.ps1
pip install -r _GCP_VM_VERSION\config\requirements\dev.txt
python _GCP_VM_VERSION\run_simulation.py --mode validate --no-gcs
python _GCP_VM_VERSION\run_simulation.py --mode single --setpoint 24 --orientation 0 --no-gcs
```

```bash
# Linux / macOS
source _GCP_VM_VERSION/.venv/bin/activate
python _GCP_VM_VERSION/run_simulation.py --mode validate --no-gcs
```

---

## 4. Execution Modes

| Mode | Simulations | Estimated time (e2-medium) | Output |
|---|---|---|---|
| `validate` | 1 | ~12 s | Log + `eplusout.err` |
| `single` | 1 | ~12 s | `result.json` |
| `gsa --n-morris 20` | ~120 | ~25 min | `gsa_results.json` |
| `gsa --n-morris 100` | ~600 | ~2 h | `gsa_results.json` |
| `pareto` | 4 | ~1 min | `pareto_front.json` |
| `exhaustive` | 192 | ~40 min | Per-candidate JSONs |
| `calibrate` | — | < 1 min | `proxy_coefficients.json`, `marginal_tables.json` |

For `gsa` with large N, consider a higher-CPU VM (N2 or N4) — see [`Contributor Guide - EnergyPlus GCP Infrastructure.md`](Contributor%20Guide%20-%20EnergyPlus%20GCP%20Infrastructure.md) §2 for sizing recommendations.

---

## 5. Outputs and Automatic Stage-Out

All modes perform automatic Stage-Out to the GCS bucket at the end, following the project convention:

```
gs://eplus-colab-cloud-data/results/gcp_vm_{mode}_{YYYYMMDD_HHMMSS}/
```

Disable with `--no-gcs` for local-only runs (see §3).

---

## 6. Dependency Layers

```
_GCP_VM_VERSION/config/requirements/
├── base.txt      # Minimum for simulation
├── research.txt  # GSA + CP-SAT + visualization (installed by setup_gcp_env.sh)
├── billing.txt   # Optional: check_billing.py BigQuery mode
├── design.txt    # Optional: Designer_Decision_Explorer.ipynb
└── dev.txt       # Local development — everything + Jupyter
```

Install:

```bash
# GCP VM — full research pipeline (default for setup_gcp_env.sh)
pip install -r _GCP_VM_VERSION/config/requirements/research.txt

# Local Windows / VS Code — everything
pip install -r _GCP_VM_VERSION/config/requirements/dev.txt
```

`setup_gcp_env.sh` accepts `--with-billing` and `--with-design` to layer the optional requirements on top of `research.txt` in a single pass.
