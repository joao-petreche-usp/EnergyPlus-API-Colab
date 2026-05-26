### GCP VM Version (VS Code + Compute Engine + Claude Code)

Hybrid workflow: local development in VS Code, execution on GCP VM via Claude Code
with MCP `gcp-compute` (29 tools). Entry point is the CLI script `run_simulation.py`
(4 modes: validate / single / gsa / pareto). Input and output data transit through
GCS bucket `eplus-colab-cloud-data` via automatic Stage-In / Stage-Out.

---

#### Current Infrastructure

| Component | Value |
|---|---|
| VM | `sim-test-vm`, `us-central1-a`, `e2-medium`, 30 GB |
| OS | Ubuntu 22.04 LTS |
| EnergyPlus | 25.1.0 at `/usr/local/EnergyPlus-25-1-0` |
| Authentication | Workload Identity — Service Account `830889929886-compute@developer.gserviceaccount.com` |
| venv | `~/EnergyPlus-API-Colab/_GCP_VM_VERSION/.venv` |
| GCS Bucket | `eplus-colab-cloud-data` (`models/`, `weather/`, `results/`) |

---

#### Prerequisites (local)

- VS Code with extensions [Cloud Code](https://marketplace.visualstudio.com/items?itemName=googlecloudtools.cloudcode)
  and [GCP Compute Engine MCP](https://marketplace.visualstudio.com/items?itemName=google.google-compute-engine-mcp-extension)
- `gcloud` CLI authenticated: `gcloud auth application-default login`
- Claude Code with MCP `gcp-compute` enabled (`.mcp.json` in repo)

---

#### Main Files

| File | Description |
|---|---|
| `run_simulation.py` | CLI entry point — 4 execution modes |
| `scripts/reprocess_gsa.py` | Re-extract GSA from existing `eplustbl.htm` files without re-simulating |
| `config/setup_gcp_env.sh` | Non-interactive VM setup (7 steps): git clone → Python 3.11 → EnergyPlus 25.1.0 → venv → deps |
| `config/requirements/base.txt` | Production VM — minimum for simulation |
| `config/requirements/research.txt` | Research VM — + SALib, OR-Tools, visualization |
| `config/requirements/dev.txt` | Local development — + Jupyter, Gemini API |
| `src/utils/idf_patcher.py` | Block-level IDF parser (geometry + envelope) |
| `src/config.py` | Constants and auto-detection of `EPLUS_DIR` |

---

#### Procedure: Create VM from scratch

**Step 1 — Create instance (Bash, 30 GB)**
```bash
gcloud compute instances create sim-test-vm \
  --zone=us-central1-a \
  --machine-type=e2-medium \
  --image-family=ubuntu-2204-lts \
  --image-project=ubuntu-os-cloud \
  --boot-disk-size=30GB \
  --project=eplus-colab-cloud
```

**Step 2 — Stop (MCP: `stop_instance`)**
- Parameters: `project=eplus-colab-cloud`, `zone=us-central1-a`, `name=sim-test-vm`
- Wait for `get_zone_operation` → `status == "DONE"`

**Step 3 — Attach service account (Bash)**
```bash
gcloud compute instances set-service-account sim-test-vm \
  --zone=us-central1-a \
  --service-account=830889929886-compute@developer.gserviceaccount.com \
  --scopes=cloud-platform \
  --project=eplus-colab-cloud
```

**Step 4 — Start (MCP: `start_instance`)**
- Wait for `get_zone_operation` → `status == "DONE"`

**Step 5 — Configure SSH (Bash)**
```bash
gcloud compute config-ssh --project=eplus-colab-cloud
```

**Connect:**
```bash
ssh sim-test-vm.us-central1-a.eplus-colab-cloud
```

---

#### Procedure: Delete VM

> **Warning:** always confirm before deleting — the operation is irreversible.

1. Confirm with user via `AskUserQuestion`
2. Execute via MCP `delete_instance` (not via `gcloud`)
3. Wait for `get_zone_operation` → `status == "DONE"` without errors

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

**3. Quick validation (~13 s):**
```bash
python _GCP_VM_VERSION/run_simulation.py --mode validate
```

**4. GSA Morris (N=20, ~25 min):**
```bash
python _GCP_VM_VERSION/run_simulation.py --mode gsa --n-morris 20
```

**5. Complete pipeline:**
```bash
python _GCP_VM_VERSION/run_simulation.py --mode pareto
```

**6. Stop VM when done (MCP: `stop_instance`)**

---

#### Execution Modes

| Mode | Simulations | Time (e2-medium) | Output |
|---|---|---|---|
| `validate` | 1 | ~13 s | log + `eplusout.err` |
| `single` | 1 | ~13 s | `result.json` |
| `gsa --n-morris 20` | ~120 | ~25 min | `gsa_results.json` |
| `gsa --n-morris 100` | ~600 | ~2 h | `gsa_results.json` |
| `pareto` | 4 | ~1 min | `pareto_front.json` |

> For GSA with large N, use VM N4 — see `docs/MCP_Extension_Architecture.md` §6.

---

#### Local Development (without GCS)

```powershell
# Windows — activate local venv
.\.venv\Scripts\Activate.ps1
pip install -r _GCP_VM_VERSION\config\requirements\dev.txt

python _GCP_VM_VERSION\run_simulation.py --mode validate --no-gcs
python _GCP_VM_VERSION\run_simulation.py --mode single --setpoint 24 --orientation 0 --no-gcs
```

---

> **Technical Note:** Workload Identity eliminates the need for `gcloud auth` inside the VM.
> PYTHONPATH is resolved internally by the script — it can be executed from any directory.
> Automatic Stage-Out to `gs://eplus-colab-cloud-data/results/gcp_vm_{mode}_{timestamp}/`.
