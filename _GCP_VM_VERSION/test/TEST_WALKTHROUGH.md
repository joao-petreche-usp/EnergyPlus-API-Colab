# /test Walkthrough — VS Code local → GCP VM → GCS Bucket

> Companion to [`TEST_PLAN.md`](TEST_PLAN.md). That file is the concise reference checklist;
> this one explains what each step does and which VS Code extension is involved.

---

## Pipeline overview

```
Windows (VS Code)
  │
  ├─ Task 1: gcloud auth (local terminal)
  │
  ├─ Task 2: Provision VM ──────────────────► GCP Compute Engine
  │           [GCE MCP Extension]                    │
  │           + attach service account               │
  │                                                  │
  ├─ Task 3: SSH + file sync ───────────────────────►│ sim-test-vm (Ubuntu 22.04, e2-medium)
  │           [Cloud Code extension]                 │
  │                                                  │
  ├─ Task 4: Set up Python env ─────────────────────►│ (python3-venv + requirements/research.txt)
  │           [SSH terminal]                         │
  │                                                  │
  ├─ Task 5: GCS round-trip test ───────────────────►│──► gs://eplus-colab-cloud-data/
  │           [SSH terminal]                         │
  │                                                  │
  ├─ Task 6: Run notebook ──────────────────────────►│ (remote kernel: .venv on VM)
  │           [VS Code + Cloud Code]                 │  EnergyPlus 25.1.0 simulation
  │                                                  │  15 result files → GCS
  │
  ├─ Task 7: Stop VM ───────────────────────► GCP (VM stopped, billing stops)
  │           [GCE MCP Extension]
  │
  └─ Task 8: Billing check (local, automated)
              python check_billing.py
```

---

## Extension roles at a glance

| VS Code Extension | What it does in this workflow |
|---|---|
| **Google Cloud Code** (`googlecloudtools.cloudcode`) | Shows GCP resources in the sidebar; provides right-click → Open SSH Terminal; enables drag-and-drop file sync to the VM |
| **GCE MCP Extension** (`google.google-compute-engine-mcp-extension`) | Lets Claude Code / the Antigravity CLI control Compute Engine via natural language — provisions and stops the VM without writing `gcloud` commands |

**MCP tools available via GCE MCP Extension:** `gcp_compute_create_instance`, `create_bucket`, `list_buckets`, `list_objects`, `get_object_metadata`, `read_object`, `read_text`, `write_text`.

---

## Task 1 — Local gcloud auth

**Where:** Windows PowerShell (no extension needed)

Two separate auth contexts are required — both must be configured before anything else:

```powershell
# ADC — used by Python libraries (google-cloud-storage, BigQuery, etc.)
gcloud auth application-default login
gcloud config set project eplus-colab-cloud
gcloud config get-value project   # confirm: eplus-colab-cloud

# CLI auth — used by gcloud commands and Cloud Code SSH
gcloud auth login
```

**Why two separate logins?** `application-default login` writes credentials for Python
SDKs (stored at `~/.config/gcloud/application_default_credentials.json`). `auth login`
authenticates the `gcloud` CLI itself — which Cloud Code uses to establish SSH sessions.
They are independent; running one does not satisfy the other.

> The VM itself does NOT need `gcloud auth`. It authenticates via a Service Account
> attached to the instance (Workload Identity) — handled automatically by GCP.

### IAM permissions required

**VM service account** (`830889929886-compute@developer.gserviceaccount.com`, scope `cloud-platform`):

| IAM Permission | Required For |
|---|---|
| `storage.objects.get` | Reading IDF and EPW inputs from GCS |
| `storage.objects.create` | Stage-Out: writing simulation results to GCS |
| `storage.objects.list` | `ls` and `mv` operations across the bucket |
| `storage.buckets.get` | Validating `eplus-colab-cloud-data` bucket availability |

**Local operator / MCP extension identity** (local gcloud ADC):

| IAM Permission | Required For |
|---|---|
| `compute.instances.start` | Starting `sim-test-vm` via Claude Code / Antigravity CLI |
| `compute.instances.stop` | Stopping `sim-test-vm` via Claude Code / Antigravity CLI |

---

## Task 2 — Provision VM

**Where:** Claude Code or Antigravity CLI chat inside VS Code
**Extension:** GCE MCP Extension

Type in the AI chat:
> "Create a new Compute Engine instance named `sim-test-vm` in zone `us-central1-a`, using Ubuntu 22.04 LTS (imageFamily: ubuntu-2204-lts, imageProject: ubuntu-os-cloud) and machine type `e2-medium`."

**If `sim-test-vm` already exists as TERMINATED**, ask instead:
> "Delete the existing instance and recreate it with Ubuntu 22.04 LTS (imageFamily: ubuntu-2204-lts, imageProject: ubuntu-os-cloud)."

**What happens under the hood:** The MCP extension translates the prompt into the
`gcp_compute_create_instance` API call and shows a confirmation dialog before executing.

> **Why Ubuntu 22.04?** The EnergyPlus installer (`EnergyPlus-25.1.0-*.sh`) is tested
> on Ubuntu and works out of the box. Debian 11 requires manual PATH adjustments.

`e2-medium` = 2 vCPUs, 4 GB RAM — sufficient for environment setup and notebook tests.

> For production simulation workloads, scale up: **N4** for sequential optimization; **C2/N2** for parallelized HPC runs.

### Attach the service account (mandatory)

Without this step, `gcloud storage` commands inside the VM will fail with permission denied.

```powershell
gcloud compute instances stop sim-test-vm --zone=us-central1-a
gcloud compute instances set-service-account sim-test-vm `
  --zone=us-central1-a `
  --service-account=830889929886-compute@developer.gserviceaccount.com `
  --scopes=cloud-platform
gcloud compute instances start sim-test-vm --zone=us-central1-a
```

Then update the local SSH config with the new instance IP:

```powershell
gcloud compute config-ssh --project=eplus-colab-cloud
```

---

## Task 3 — SSH + clone repository

**Where:** VS Code sidebar + Remote SSH
**Extension:** Cloud Code

1. Open the **Cloud Code panel** in the VS Code sidebar
2. Expand **Compute Engine** — you'll see `sim-test-vm` listed
3. Right-click `sim-test-vm` → **Open SSH Terminal**
   - VS Code opens an integrated terminal inside the Linux VM
4. Clone the repository directly on the VM (public repo — no credentials needed):

```bash
cd ~
git clone https://github.com/joao-petreche-usp/EnergyPlus-API-Colab.git
ls ~/EnergyPlus-API-Colab/   # confirm: _GCP_VM_VERSION/ is present
```

This is simpler and more reproducible than drag-and-drop: any researcher can replicate
the environment with a single command, and future updates are a `git pull` away.

> **Kernel tip:** For the notebook kernel to resolve `${workspaceFolder}/.venv` correctly,
> open VS Code in the VM at the `_GCP_VM_VERSION/` folder level (not the repo root).
> A dedicated `_GCP_VM_VERSION/.vscode/settings.json` handles this case automatically.

---

## Task 4 — Python environment on VM

**Where:** VS Code integrated SSH terminal (inside the Linux VM)

Ubuntu 22.04 does not include `python3-venv` by default — install it first:

```bash
sudo apt update && sudo apt install -y python3-venv python3-pip
```

Then choose one of the following options:

**Option A — automated (recommended):** runs `setup_gcp_env.sh`
```bash
cd ~/EnergyPlus-API-Colab/_GCP_VM_VERSION
bash config/setup_gcp_env.sh
```

The script performs 8 steps automatically:
1. Validates Google Cloud SDK and adds it to `$PATH`
2. Checks available RAM (warns if < 1 GB free)
3. Kills zombie Python processes to free memory
4. Sets the gcloud project to `eplus-colab-cloud`
5. Creates `.venv` if it doesn't exist
6. Installs dependencies from `config/requirements/research.txt` via pip
7. Checks that ADC is present at `~/.config/gcloud/application_default_credentials.json`
8. Runs `scripts/billing_report.py` and prints an infrastructure status summary

**Option B — manual equivalent:**
```bash
cd ~/EnergyPlus-API-Colab/_GCP_VM_VERSION
python3 -m venv .venv
source .venv/bin/activate
pip install -r config/requirements/research.txt
```

> No `gcloud auth` is needed inside the VM — the service account (Task 2) handles
> GCP authentication automatically via Workload Identity.

---

## Task 5 — GCS Data Lake round-trip test

**Where:** VS Code integrated SSH terminal (still inside the VM)

```bash
# Verify expected GCS bucket structure
gcloud storage ls gs://eplus-colab-cloud-data/
# Expected: models/, weather/, results/, scripts/, notebooks/

# Stage-Out: write a file to the GCS bucket
echo "EnergyPlus-API-Colab infra test" > test_result.txt
gcloud storage cp test_result.txt gs://eplus-colab-cloud-data/results/

# Stage-In: read it back and verify the content
gcloud storage cp gs://eplus-colab-cloud-data/results/test_result.txt /tmp/
cat /tmp/test_result.txt
# Expected output: EnergyPlus-API-Colab infra test
```

**What this validates:** The VM can both write to and read from the GCS bucket —
confirming the full Stage-Out (simulation results → bucket) and Stage-In
(bucket → VM) data path that EnergyPlus simulation runs depend on.

> `gcloud storage` (not `gsutil`) is used here — up to 94% faster for large
> IDF/EPW volumes due to its JSON API and DAG-based task engine.

> In a real simulation run, Stage-Out produces: `eplustbl.htm` (primary HTML performance
> report), `eplusout.csv` (zone temperature data), `eplusout.err` (error log — inspect
> this first via "Validation" before committing resources to complex workloads).

---

## Task 6 — Run a validation simulation

**Where:** VS Code integrated SSH terminal (inside the Linux VM)

With the venv still active, run:

```bash
python _GCP_VM_VERSION/run_simulation.py --mode validate
```

Expected output (log format — timestamps and level will vary):
```
14:23:45 | INFO     | ✅ Annual HVAC Energy: 64519.5 kWh
14:23:58 | INFO     | ✅ Validation OK — ready to scale (gsa or pareto mode).
```

Run time: ~13 s. EnergyPlus 25.1.0 via pyenergyplus Python API. Stage-Out uploads results to
`gs://eplus-colab-cloud-data/results/gcp_vm_validate_<timestamp>/`.

**What this validates:** Python on the VM calls EnergyPlus directly via the API (not subprocess),
authenticates to GCP via Workload Identity, and archives outputs to GCS — the full production path.

> **Legacy alternative:** `EnergyPlus_API_GCP_VM.ipynb` achieves the same result via a remote
> VS Code kernel — preserved for reference. The CLI script (`run_simulation.py`) is the
> primary path for all modes (validate / single / gsa / pareto).

---

## Task 7 — Stop VM

**Where:** Claude Code or Antigravity CLI chat inside VS Code
**Extension:** GCE MCP Extension

Type in the AI chat:
> "Stop the instance `sim-test-vm` in zone `us-central1-a`."

**Why this matters:** Compute Engine VMs are billed by the minute even when idle.
Stopping (not deleting) the VM halts compute billing while preserving the disk,
EnergyPlus installation, and Python environment for the next session.

> **Note:** stopping the VM releases its external IP. Before the next SSH session, run:
> ```powershell
> gcloud compute config-ssh --project=eplus-colab-cloud
> ```

---

## Task 8 — Billing check

**Where:** Windows PowerShell (local, automated)

```powershell
python .\_GCP_VM_VERSION\scripts\check_billing.py
```

Uses system Python — `check_billing.py` only requires stdlib + `gcloud` CLI (no venv needed).
Expected output: `Status: ACTIVE | Account: 018BDF-F25C35-6646B4`

**What this validates:** Confirms the billing account is active and closes the FinOps loop
for the test session.

---

## Troubleshooting

**Cloud Code SSH fails (OAuth / unexpected error)**
→ Run `gcloud auth login` in PowerShell. ADC (`application-default login`) and gcloud CLI
auth are separate sessions — running one does not satisfy the other.

**`gcloud storage ls` fails inside VM (permission denied)**
→ Service account not attached to the instance. Stop VM → `set-service-account --scopes=cloud-platform` → start VM. See Task 2.

**`python3 -m venv .venv` fails with "ensurepip is not available"**
→ Run `sudo apt install -y python3-venv python3-pip` first.

**VS Code Remote SSH "Could not establish connection"**
→ VM IP changed after restart. Run `gcloud compute config-ssh --project=eplus-colab-cloud` locally and retry.

**Wrong kernel / interpreter not found in notebook**
→ Open VS Code at `_GCP_VM_VERSION/` level (not repo root) so `${workspaceFolder}` resolves to `.venv`.

**`check_billing.py` returns "Critical failure" on Windows**
→ Verify `gcloud` is in PATH: `gcloud --version`. The script uses `shell=True` to find `gcloud.cmd` on Windows.

---

## Further reading

| Topic | File |
|---|---|
| Concise test reference checklist | [`TEST_PLAN.md`](TEST_PLAN.md) |
| `/test` command definition (Claude) | [`.claude/commands/test.md`](../../.claude/commands/test.md) |
| GCP VM setup and connection guide | [`.claude/docs/gcp-vm-setup.md`](../../.claude/docs/gcp-vm-setup.md) |
| GCE MCP Extension setup | [`.claude/docs/gcp-compute-engine-mcp.md`](../../.claude/docs/gcp-compute-engine-mcp.md) |
