# Test Plan — Hybrid Infrastructure EnergyPlus + GCP

Objective: validate the complete pipeline **VS Code local → GCP VM → GCS Bucket**,
using the `EnergyPlus_API_GCP_VM.ipynb` notebook as orchestrator.

Project: `eplus-colab-cloud` | VM: `sim-test-vm` | Zone: `us-central1-a` | Bucket: `eplus-colab-cloud-data`

Required VS Code extensions (already installed):
- Google Cloud Code (`googlecloudtools.cloudcode`)
- Google Compute Engine MCP Extension (`google.google-compute-engine-mcp-extension`)

---

## Local Precondition (Windows)

Two separate gcloud auth contexts are required:

```powershell
# ADC — used by Python libraries (google-cloud-storage, BigQuery, etc.)
gcloud auth application-default login
gcloud config set project eplus-colab-cloud

# CLI auth — used by gcloud commands and Cloud Code SSH
gcloud auth login
```

### IAM Permissions Required

**VM Service Account** (`830889929886-compute@developer.gserviceaccount.com`, scope `cloud-platform`):

| IAM Permission | Required For |
| ------ | ------ |
| `storage.objects.get` | Reading IDF (model) and EPW (weather) inputs |
| `storage.objects.create` | Stage-Out operations and result archival |
| `storage.objects.list` | `ls` and `mv` operations across the bucket |
| `storage.buckets.get` | Validating `eplus-colab-cloud-data` availability |

**Local operator / MCP extension identity** (local gcloud ADC):

| IAM Permission | Required For |
| ------ | ------ |
| `compute.instances.start` | Starting `sim-test-vm` via Claude Code / Antigravity CLI |
| `compute.instances.stop` | Stopping `sim-test-vm` via Claude Code / Antigravity CLI |

---

## Step 1 — Provision VM via GCE MCP Extension

In the **Claude Code** or **Antigravity CLI** chat in VS Code:

> "Create a new Compute Engine instance named `sim-test-vm` in zone `us-central1-a`, using Ubuntu 22.04 LTS (imageFamily: ubuntu-2204-lts, imageProject: ubuntu-os-cloud) and machine type `e2-medium`."

**If `sim-test-vm` already exists as TERMINATED**, ask instead:
> "Delete the existing instance and recreate it with Ubuntu 22.04 LTS (imageFamily: ubuntu-2204-lts, imageProject: ubuntu-os-cloud)."

After the VM is RUNNING, attach the service account (required for GCS access — without this, `gcloud storage` commands inside the VM will fail):

```powershell
gcloud compute instances stop sim-test-vm --zone=us-central1-a
gcloud compute instances set-service-account sim-test-vm `
  --zone=us-central1-a `
  --service-account=830889929886-compute@developer.gserviceaccount.com `
  --scopes=cloud-platform
gcloud compute instances start sim-test-vm --zone=us-central1-a
```

Update the local SSH config with the new instance IP:

```powershell
gcloud compute config-ssh --project=eplus-colab-cloud
```

> `e2-medium` is sized for environment setup and notebook tests only. For production simulation workloads, use N4 (sequential optimization) or C2/N2 (parallelized HPC runs).

---

## Step 2 — Connect via SSH and Clone Repository

1. In the **Cloud Code** panel → right-click `sim-test-vm` → **Open SSH Terminal**
2. In the SSH terminal, clone the repository (public — no credentials needed):

```bash
cd ~
git clone https://github.com/joao-petreche-usp/EnergyPlus-API-Colab.git
ls ~/EnergyPlus-API-Colab/   # confirm: _GCP_VM_VERSION/ is present
```

> **Kernel tip:** For the notebook kernel to resolve `${workspaceFolder}/.venv` correctly,
> open VS Code in the VM at the `_GCP_VM_VERSION/` folder level (not the repo root).
> A dedicated `.vscode/settings.json` inside `_GCP_VM_VERSION/` handles this case automatically.

---

## Step 3 — Set Up the VM Linux Environment

In the VS Code integrated SSH terminal:

```bash
# Ubuntu 22.04 does not include python3-venv by default
sudo apt update && sudo apt install -y python3-venv python3-pip
```

Then choose one of the following options:

**Option A — automated script (recommended):**
```bash
cd ~/EnergyPlus-API-Colab/_GCP_VM_VERSION
bash config/setup_gcp_env.sh
```

**Option B — manual:**
```bash
cd ~/EnergyPlus-API-Colab/_GCP_VM_VERSION
python3 -m venv .venv
source .venv/bin/activate
pip install -r config/requirements/research.txt
```

> No `gcloud auth` is needed inside the VM — the attached service account (Step 1) handles
> GCP authentication automatically via Workload Identity.

---

## Step 4 — Validate the Data Lake (Stage-In / Stage-Out)

```bash
# Verify expected GCS bucket structure
gcloud storage ls gs://eplus-colab-cloud-data/
# Expected: models/, weather/, results/, scripts/, notebooks/

# Stage-Out: send result to bucket
echo "EnergyPlus-API-Colab infra test" > test_result.txt
gcloud storage cp test_result.txt gs://eplus-colab-cloud-data/results/

# Stage-In: download back and verify
gcloud storage cp gs://eplus-colab-cloud-data/results/test_result.txt /tmp/
cat /tmp/test_result.txt
# Expected output: EnergyPlus-API-Colab infra test
```

> Uses `gcloud storage` (not `gsutil`) — up to 94% faster for large volumes of IDFs/EPWs.

> In a real simulation run, Stage-Out produces: `eplustbl.htm` (performance report),
> `eplusout.csv` (zone temperature data), `eplusout.err` (error log — use for
> "Validation" before scaling to complex workloads).

---

## Step 5 — Run a Validation Simulation

In the SSH terminal (venv still active):

```bash
python _GCP_VM_VERSION/run_simulation.py --mode validate
```

Expected output (log format — timestamps and level will vary):
```
14:23:45 | INFO     | ✅ Annual HVAC Energy: 64519.5 kWh
14:23:58 | INFO     | ✅ Validation OK — ready to scale (gsa or pareto mode).
```

Run time: ~13 s. EnergyPlus 25.1.0. Zero warnings, zero errors.

> **Legacy alternative:** the notebook `EnergyPlus_API_GCP_VM.ipynb` achieves the
> same result via a remote VS Code kernel — preserved for reference but not the primary test path.

---

## Step 6 — Stop the VM (save credits)

In the Claude Code or Antigravity CLI chat (MCP):

> "Stop the instance `sim-test-vm` in zone `us-central1-a`."

> **Note:** stopping the VM releases its external IP. Before the next SSH session, run:
> `gcloud compute config-ssh --project=eplus-colab-cloud`

---

## Step 7 — Post-test Cost Check (local)

```powershell
python .\_GCP_VM_VERSION\scripts\check_billing.py
```

> Uses system Python — `check_billing.py` only requires stdlib + `gcloud` CLI (no venv needed).
> Expected output: `Status: ACTIVE | Account: 018BDF-F25C35-6646B4`

---

## Pending Items

- [x] Add `.venv-test/` to the project `.gitignore`
- [x] Fix `check_billing.py`: remove `gen-lang-client` project, add `shell=True` for Windows
- [x] Fix `.vscode/settings.json`: correct interpreter path for repo root and `_GCP_VM_VERSION/` contexts

---

## Troubleshooting

**Cloud Code SSH fails (OAuth error)**
→ Run `gcloud auth login` in PowerShell. ADC and gcloud CLI auth are separate.

**`gcloud storage ls` fails inside VM (permission denied)**
→ Service account not attached. Stop VM → `set-service-account --scopes=cloud-platform` → start VM.

**`python3 -m venv .venv` fails**
→ Run `sudo apt install -y python3-venv python3-pip` first.

**VS Code Remote SSH "Could not establish connection"**
→ VM IP changed after restart. Run `gcloud compute config-ssh --project=eplus-colab-cloud`.

**Wrong kernel / interpreter not found**
→ Open VS Code at `_GCP_VM_VERSION/` level, not repo root.
