# Deployment Guide — `run_simulation.py`

Operational reference for the EnergyPlus + CP-SAT pipeline. The high-level Quick Start is in the [project README](../../README.md); this document covers (1) invocation patterns, (2) the recommended local `--no-gcs` workflow, (3) the GCP VM workflow for the large GSA campaign, (4) the canonical execution-mode table, (5) Stage-Out paths, (6) dependency layers, and (7) common troubleshooting.

---

## 1. Invocation patterns

`PYTHONPATH` is resolved internally — the script adds `_GCP_VM_VERSION/` to `sys.path` automatically, so it runs from any directory:

```bash
# From the repository root (recommended)
cd EnergyPlus-API-Colab
python -u _GCP_VM_VERSION/run_simulation.py --mode validate --no-gcs

# From inside _GCP_VM_VERSION/
cd EnergyPlus-API-Colab/_GCP_VM_VERSION
python -u run_simulation.py --mode validate --no-gcs

# Via an absolute path
python -u ~/EnergyPlus-API-Colab/_GCP_VM_VERSION/run_simulation.py --mode validate --no-gcs
```

The `-u` flag disables Python output buffering — required to see real-time progress for long batch runs (`gsa-sobol`, `exhaustive`).

---

## 2. Local workflow (`--no-gcs`) — recommended for paper reproduction

The `--no-gcs` flag bypasses every GCS read/write and uses the IDF, weather file, and pre-computed ground-truth Pareto reference shipped under `_GCP_VM_VERSION/data/`. This is the path the manuscript's Data Availability section points to.

### 2.1 Prerequisites

| Component | How to obtain |
|---|---|
| Python 3.10+ | <https://www.python.org/downloads/> |
| EnergyPlus 25.1.0 | <https://energyplus.net/downloads> — install at `/usr/local/EnergyPlus-25-1-0`, `/eplus`, `~/eplus`, or `C:\EnergyPlusV25-1-0`. Custom path: set `EPLUS_DIR=...`. |
| `pyenergyplus` (Python API) | Ships with EnergyPlus — added to `sys.path` by `src/config.py` when needed; **do not pip-install**. |

### 2.2 Environment setup (once)

```bash
git clone https://github.com/joao-petreche-usp/EnergyPlus-API-Colab.git
cd EnergyPlus-API-Colab
python -m venv .venv
source .venv/bin/activate                                        # Windows: .venv\Scripts\Activate.ps1
pip install -r _GCP_VM_VERSION/config/requirements/research.txt
```

### 2.3 Reproduce Section 4 of the paper

```bash
# Single-design sanity check (~10 s)
python -u _GCP_VM_VERSION/run_simulation.py --mode validate --no-gcs
# Expected: Annual HVAC Energy: 64519.5 kWh (Exact match within tolerance)

# Pareto front + M3.9 benchmark (64 sims, ~1.5 min on 8 cores)
python -u _GCP_VM_VERSION/run_simulation.py --mode pareto-sequential --workers 8 --no-gcs
# Expected: Effectiveness 100.0%, HV ratio 1.0001, 66.7% sim reduction
```

The M3.9 benchmark reads `_GCP_VM_VERSION/data/exhaustive_pareto_ref.json` (57 KB, 192-sim ground truth) automatically when `--no-gcs` is set. No manual file staging is needed.

### 2.4 Reproduce Tables 1 & 2 (Sobol-Saltelli, optional)

`--mode gsa-sobol` re-runs the full 768-simulation Saltelli campaign. Allow ~3 hours on a quad-core laptop or ~25 minutes on a 16-core machine.

```bash
python -u _GCP_VM_VERSION/run_simulation.py --mode gsa-sobol --n-sobol 128 --workers 16 --no-gcs
```

Pre-computed outputs are already in `_GCP_VM_VERSION/data/gsa_results_v2.{json,csv}` and `_GCP_VM_VERSION/data/sobol_indices_n128.json` — the README tables are derived from these.

---

## 3. GCP VM workflow

The canonical environment for the published timings (`~25 min` for the 768-sim GSA campaign on 16 cores). Skip this section if you are only reproducing Section 4.

### 3.1 Prerequisites

- [Google Cloud SDK](https://cloud.google.com/sdk/docs/install)
- A GCP project with billing enabled
- The compute service account attached with `cloud-platform` scope

### 3.2 Provision the VM (one-time)

```powershell
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

For a 16-core machine that finishes the 768-sim GSA in ~25 min, use `--machine-type=n2-standard-16` instead.

### 3.3 Bootstrap the VM (one-time)

```bash
ssh sim-test-vm.us-central1-a.YOUR-PROJECT
git clone https://github.com/joao-petreche-usp/EnergyPlus-API-Colab.git
cd ~/EnergyPlus-API-Colab
bash _GCP_VM_VERSION/config/setup_gcp_env.sh
source _GCP_VM_VERSION/.venv/bin/activate
```

`setup_gcp_env.sh` installs EnergyPlus 25.1.0, creates `.venv`, and installs `requirements/research.txt`.

### 3.4 Reproduce the full pipeline

```bash
git pull origin main
python -u _GCP_VM_VERSION/run_simulation.py --mode validate                # ~10 s
python -u _GCP_VM_VERSION/run_simulation.py --mode gsa-sobol --workers 16  # ~25 min, Sobol indices
python -u _GCP_VM_VERSION/run_simulation.py --mode exhaustive --workers 16 # ~10 min, 192-sim ground truth
python -u _GCP_VM_VERSION/run_simulation.py --mode pareto-sequential --workers 16  # ~30 s, Pareto front
```

Omit `--no-gcs` here so each run streams results to `gs://eplus-colab-cloud-data/results/gcp_vm_<mode>_<timestamp>/` (Stage-Out, §5).

### 3.5 Stop the VM

```powershell
gcloud compute instances stop sim-test-vm --zone=us-central1-a
```

---

## 4. Execution modes

| Mode | EnergyPlus sims | Time (e2-standard-8, 8 workers) | Output JSON |
|---|---|---|---|
| `validate` | 1 | ~10 s | `eplusout.err` + log; ground-truth check (HVAC = 64519.5 kWh) |
| `single` | 1 | ~10 s | `result.json` — one design point of your choosing (`--setpoint`, `--orientation`, …) |
| `gsa` | ~120 (Morris, `--n-morris 20`) | ~25 min | `gsa_results.json` — Morris elementary effects (optional smoke test for GSA pipeline) |
| `gsa-sobol` | 768 (Saltelli, `--n-sobol 128`) | ~3 h sequential / ~25 min on 16 cores | `gsa_results_v2.{json,csv}`, `sobol_indices_n128.json` — **Tables 1 & 2 of the paper** |
| `exhaustive` | 192 (full factorial) | ~40 min on 8 cores | `exhaustive_pareto_ref.json` — **§4.1 ground truth** |
| `pareto` | 4 | ~1 min | `pareto_front.json` — block-1 isolated (smoke) |
| `pareto-sequential` | 64 (16 block-1 + 48 block-2) | ~1.5 min | `pareto_sequential_<timestamp>.json` — **§4.2 sequential front + M3.9 benchmark** |
| `calibrate` | 0 (uses existing GSA samples) | < 1 min | `proxy_coefficients.json`, `marginal_tables.json` — CP-SAT linearisation |

`--workers N` parallelises the EnergyPlus runs over a process pool — defaults to `os.cpu_count()`. Leave 1-2 cores for OS / I/O on machines with ≥ 16 cores.

For sizing recommendations on multi-hour GSA campaigns, see [`Contributor Guide - EnergyPlus GCP Infrastructure.md`](Contributor%20Guide%20-%20EnergyPlus%20GCP%20Infrastructure.md) §2.

---

## 5. Stage-Out paths (cloud only)

When **not** using `--no-gcs`, every mode performs an automatic Stage-Out to the project bucket at the end:

```
gs://eplus-colab-cloud-data/results/gcp_vm_<mode>_<YYYYMMDD_HHMMSS>/
```

Override the bucket with `--bucket=other-bucket-name`. Disable entirely with `--no-gcs` (§2).

---

## 6. Dependency layers

```
_GCP_VM_VERSION/config/requirements/
├── base.txt      # Minimum to run a single EnergyPlus simulation
├── research.txt  # base + GSA (SALib) + CP-SAT (OR-Tools) + visualisation — recommended
├── billing.txt   # Optional: check_billing.py BigQuery integration
├── design.txt    # Optional: Designer_Decision_Explorer.ipynb extras
└── dev.txt       # Local development — research + jupyter + linters
```

Install for paper reproduction:

```bash
pip install -r _GCP_VM_VERSION/config/requirements/research.txt
```

`setup_gcp_env.sh` (GCP path) accepts `--with-billing` and `--with-design` to layer the optional requirements on top of `research.txt` in a single pass.

---

## 7. Troubleshooting

**`ImportError: cannot import name 'pyenergyplus'`**
The Python API ships with EnergyPlus, not pip. Confirm EnergyPlus 25.1.0 is installed at one of the auto-detected paths or set `EPLUS_DIR=/path/to/EnergyPlus-25-1-0`.

**`Ground-truth file not found — M3.9 benchmark skipped`**
This warning should never appear in `--no-gcs` mode (the fallback to `_GCP_VM_VERSION/data/exhaustive_pareto_ref.json` is automatic). If you see it on the GCP path, re-run `--mode exhaustive --workers 16` first to populate `gs://.../results/gcp_vm_exhaustive_<timestamp>/exhaustive_pareto_ref.json` or pass `--ground-truth-gcs <gs://path>`.

**Windows `pip install` SSL `CERTIFICATE_VERIFY_FAILED`**
Caused by a corporate antivirus MITM (Norton, etc.) replacing the pip TLS chain. Workaround:

```powershell
pip install -r _GCP_VM_VERSION\config\requirements\research.txt `
  --trusted-host pypi.org --trusted-host files.pythonhosted.org
```

**`EnergyPlus failed to start` on Linux**
Verify the binary is executable and on PATH: `/usr/local/EnergyPlus-25-1-0/energyplus --version` should print `EnergyPlus, Version 25.1.0`.

**Multi-processing hangs on Windows**
EnergyPlus child processes inherit the Python interpreter — Windows' `spawn` start method requires the entry point to be guard-protected. The shipped `run_simulation.py` already does this; if you embed it in your own script, wrap your `main()` call in `if __name__ == "__main__":`.
