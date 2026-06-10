### GCP VM Version (VS Code + Compute Engine)

Entry point is the CLI script `run_simulation.py`
(6 modes: validate / single / gsa / pareto / exhaustive / pareto-sequential). Input and output data transit through
GCS bucket `eplus-colab-cloud-data` via automatic Stage-In / Stage-Out.

---

#### Current Infrastructure

| Component | Value |
|---|---|
| VM | `sim-test-vm`, `us-central1-a`, `e2-standard-8`, 30 GB |
| OS | Ubuntu 22.04 LTS |
| EnergyPlus | 25.1.0 at `/usr/local/EnergyPlus-25-1-0` |
| Authentication | Workload Identity — Service Account `YOUR-PROJECT-NUMBER-compute@developer.gserviceaccount.com` |
| venv | `~/EnergyPlus-API-Colab/_GCP_VM_VERSION/.venv` |
| GCS Bucket | `eplus-colab-cloud-data` (`models/`, `weather/`, `results/`) |

---

#### Prerequisites (local)

- `gcloud` CLI authenticated: `gcloud auth application-default login`
- GCP project with billing enabled

---

#### Main Files

| File | Description |
|---|---|
| `run_simulation.py` | CLI entry point — 6 execution modes |
| `scripts/reprocess_gsa.py` | Re-extract GSA from existing `eplustbl.htm` files without re-simulating |
| `scripts/build_proxies.py` | Fit CP-SAT linearization proxies from a calibrate run |
| `config/setup_gcp_env.sh` | Non-interactive VM setup (7 steps): git clone → Python 3.11 → EnergyPlus 25.1.0 → venv → deps |
| `config/requirements/base.txt` | Production VM — minimum for simulation |
| `config/requirements/research.txt` | Research VM — + SALib, OR-Tools, visualization |
| `config/requirements/dev.txt` | Local development — + Jupyter |
| `src/utils/idf_patcher.py` | Block-level IDF parser (geometry + envelope) |
| `src/config.py` | Constants and auto-detection of `EPLUS_DIR` |
| `data/sobol_exact.json` | ★ Sobol exact GSA (192-sim full factorial, 4 DPs) |
| `data/reference_results.json` | ★ Canonical Pareto front (4 designs, 64 sims, 100% effectiveness) |

---

#### Procedure: Create VM from scratch

**Step 1 — Create instance (30 GB)**
```bash
gcloud compute instances create sim-test-vm \
  --zone=us-central1-a \
  --machine-type=e2-standard-8 \
  --image-family=ubuntu-2204-lts \
  --image-project=ubuntu-os-cloud \
  --boot-disk-size=30GB \
  --project=YOUR-PROJECT-ID
```

**Step 2 — Stop, attach service account, start**
```bash
gcloud compute instances stop sim-test-vm --zone=us-central1-a
gcloud compute instances set-service-account sim-test-vm \
  --zone=us-central1-a \
  --service-account=YOUR-PROJECT-NUMBER-compute@developer.gserviceaccount.com \
  --scopes=cloud-platform
gcloud compute instances start sim-test-vm --zone=us-central1-a
gcloud compute config-ssh --project=YOUR-PROJECT-ID
```

**Connect:**
```bash
ssh sim-test-vm.us-central1-a.YOUR-PROJECT-ID
```

---

#### Execution Instructions

**1. Configure VM after creation:**
```bash
# On VM (via SSH):
git clone https://github.com/joao-petreche-usp/EnergyPlus-API-Colab.git
cd EnergyPlus-API-Colab
bash _GCP_VM_VERSION/config/setup_gcp_env.sh
```

**2. Activate venv and install research dependencies:**
```bash
source _GCP_VM_VERSION/.venv/bin/activate
pip install -r _GCP_VM_VERSION/config/requirements/research.txt
```

**3. Quick validation (~14 s):**
```bash
python _GCP_VM_VERSION/run_simulation.py --mode validate
```

**4. Full factorial ground truth (192 sims, ~25 min on e2-standard-8):**
```bash
tmux new -s exhaustive
python _GCP_VM_VERSION/run_simulation.py --mode exhaustive --quiet
```

**5. Sequential Pareto front (64 sims, ~8 min):**
```bash
python _GCP_VM_VERSION/run_simulation.py --mode pareto-sequential --quiet
```

**6. Stop VM when done:**
```bash
gcloud compute instances stop sim-test-vm --zone=us-central1-a
```

---

#### Execution Modes

| Mode | Simulations | Time (e2-standard-8) | Output |
|---|---|---|---|
| `validate` | 1 | ~14 s | log + `eplusout.err` |
| `single` | 1 | ~14 s | `result.json` |
| `gsa --n-morris 20` | ~120 | ~4 min | `gsa_results.json` |
| `exhaustive` | 192 | ~25 min | `exhaustive_pareto.json` (ground truth) |
| `pareto-sequential` | 64 | ~8 min | `pareto_sequential.json` (100% effectiveness) |
| `calibrate` | ~64 | ~8 min | proxy coefficient files for CP-SAT |

> For long runs (`exhaustive`, `gsa`), use `tmux` to survive SSH disconnection.

---

> **Technical Note:** Workload Identity eliminates the need for `gcloud auth` inside the VM.
> PYTHONPATH is resolved internally by the script — it can be executed from any directory.
> Automatic Stage-Out to `gs://eplus-colab-cloud-data/results/gcp_vm_{mode}_{timestamp}/`.
