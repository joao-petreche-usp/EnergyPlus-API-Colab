#!/usr/bin/env bash
# =============================================================================
# setup_gcp_env.sh — EnergyPlus-API-Colab GCP VM Setup (v4)
# =============================================================================
# Usage:
#   cd ~/EnergyPlus-API-Colab
#   bash _GCP_VM_VERSION/config/setup_gcp_env.sh [--with-billing] [--with-design]
#
#   --with-billing  also install requirements/billing.txt (google-cloud-bigquery
#                   etc.) so check_billing.py can query real costs on the VM.
#   --with-design   also install requirements/design.txt (plotly, ipykernel) for
#                   the Designer Decision Explorer notebook (the /design process).
#
# Environment overrides:
#   REPO_URL        Git URL to clone from. Default is the private -dev repo
#                   (development scope). Public override for paper-reproducibility:
#                     REPO_URL=https://github.com/joao-petreche-usp/EnergyPlus-API-Colab.git \
#                       bash _GCP_VM_VERSION/config/setup_gcp_env.sh
#                   Cloning the private -dev repo requires GitHub auth on the VM
#                   (e.g. `gh auth login` before invoking this script).
#                   On previously-provisioned VMs, the script will also re-point
#                   the existing origin to $REPO_URL when they differ.
#   PYTHON_VERSION  Python major.minor used for the venv (default: 3.11). Ubuntu
#                   22.04 ships 3.10 in its default repos; other versions are
#                   installed via the deadsnakes PPA on demand. Pin to 3.10 only
#                   if you need to reproduce the pre-v4 environment exactly.
#
# Changes in v4:
#   - PYTHON_VERSION is configurable (default: 3.11). google-cloud-* libraries
#     warn that Python 3.10 support ends 2026-10-04; 3.11 gives runway. The
#     script installs the chosen Python from the deadsnakes PPA when not present.
#   - Step 6 (energyplus.pth) derives the site-packages path from PYTHON_VERSION
#     instead of hardcoding python3.10.
#
# Changes in v3:
#   - REPO_URL is now configurable via env var (default: -dev). Previously the
#     script hardcoded the public repo URL, leaving VMs on the public scope
#     and missing post-2026-05-22 development work (M3.7+).
#   - On already-provisioned VMs, the script now re-points the existing origin
#     to REPO_URL when they differ, so a VM bootstrapped from the public repo
#     can be migrated to the -dev repo by re-running this script.
# Changes in v2:
#   - Uses .sh installer (not .run) for EnergyPlus 25.1.0
#   - printf 'y\n\nn\n' for non-interactive mode
#   - EPLUS_INSTALL=/usr/local/EnergyPlus-25-1-0 (installer default)
#   - 7 steps with full validation
# =============================================================================

set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✅ $*${NC}"; }
warn() { echo -e "${YELLOW}⚠️  $*${NC}"; }
fail() { echo -e "${RED}❌ $*${NC}"; exit 1; }
step() { echo -e "\n${YELLOW}── $* ──────────────────────────────${NC}"; }

REPO_DIR="$HOME/EnergyPlus-API-Colab"
REPO_URL="${REPO_URL:-https://github.com/joao-petreche-usp/EnergyPlus-API-Colab.git}"
PYTHON_VERSION="${PYTHON_VERSION:-3.11}"
PYTHON_BIN="python${PYTHON_VERSION}"
VENV_DIR="$REPO_DIR/_GCP_VM_VERSION/.venv"
EPLUS_VERSION="25.1.0"
EPLUS_SHA="68a4a7c774"
EPLUS_INSTALL="/usr/local/EnergyPlus-25-1-0"
EPLUS_URL="https://github.com/NREL/EnergyPlus/releases/download/v${EPLUS_VERSION}/EnergyPlus-${EPLUS_VERSION}-${EPLUS_SHA}-Linux-Ubuntu22.04-x86_64.sh"
REQUIREMENTS="$REPO_DIR/_GCP_VM_VERSION/config/requirements/research.txt"
BILLING_REQUIREMENTS="$REPO_DIR/_GCP_VM_VERSION/config/requirements/billing.txt"
DESIGN_REQUIREMENTS="$REPO_DIR/_GCP_VM_VERSION/config/requirements/design.txt"

# Optional extras — enabled by flags (any order, combinable)
WITH_BILLING=0
WITH_DESIGN=0
for arg in "$@"; do
    case "$arg" in
        --with-billing) WITH_BILLING=1 ;;
        --with-design)  WITH_DESIGN=1 ;;
        *) fail "Unknown argument: $arg (expected --with-billing and/or --with-design)" ;;
    esac
done

echo "============================================================"
echo " EnergyPlus-API-Colab — GCP VM Setup v4 (Python ${PYTHON_VERSION})"
echo " $(date '+%Y-%m-%d %H:%M:%S UTC')"
echo "============================================================"

# ── Step 1: System dependencies ───────────────────────────────────────────────
step "1/7 System dependencies"
sudo apt-get update -qq
sudo apt-get install -y -qq \
    python3-pip software-properties-common \
    git wget curl \
    libx11-6 libexpat1 libgl1 \
    libxcb-icccm4 libxcb-image0 libxcb-keysyms1 \
    libxcb-render-util0 libxcb-xinerama0 libxcb-xkb1 \
    libxkbcommon-x11-0
ok "System dependencies installed"

# Install requested Python interpreter (deadsnakes PPA when not in default repos)
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "   ${PYTHON_BIN} not found; adding deadsnakes PPA..."
    sudo add-apt-repository -y ppa:deadsnakes/ppa
    sudo apt-get update -qq
fi
sudo apt-get install -y -qq \
    "${PYTHON_BIN}" "${PYTHON_BIN}-venv" "${PYTHON_BIN}-dev" \
    || fail "Could not install ${PYTHON_BIN}. Check the active deadsnakes versions at https://launchpad.net/~deadsnakes/+archive/ubuntu/ppa and pin PYTHON_VERSION accordingly."
ok "Python interpreter: $(${PYTHON_BIN} --version)"

# ── Step 2: Repository ───────────────────────────────────────────────────────
step "2/7 Repository"
if [ -d "$REPO_DIR" ]; then
    cd "$REPO_DIR"
    CURRENT_URL=$(git remote get-url origin 2>/dev/null || echo "")
    if [ -n "$CURRENT_URL" ] && [ "$CURRENT_URL" != "$REPO_URL" ]; then
        warn "Existing origin '$CURRENT_URL' differs from requested REPO_URL"
        echo "   Re-pointing origin → $REPO_URL"
        git remote set-url origin "$REPO_URL"
    fi
    git pull origin main --quiet \
        || fail "Pull failed. For the private -dev repo, run 'gh auth login' on the VM first, or set REPO_URL=<public-repo-url>"
    ok "Repository updated: $REPO_DIR (origin=$(git remote get-url origin))"
else
    echo "   Cloning from: $REPO_URL"
    git clone --depth 1 "$REPO_URL" "$REPO_DIR" \
        || fail "Clone failed. For the private -dev repo, run 'gh auth login' on the VM first, or set REPO_URL=<public-repo-url>"
    ok "Repository cloned: $REPO_DIR"
fi
cd "$REPO_DIR"

# ── Step 3: Virtual environment ───────────────────────────────────────────────
step "3/7 Python virtual environment"
[ -d "$VENV_DIR" ] && rm -rf "$VENV_DIR"
"$PYTHON_BIN" -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"
pip install --upgrade pip --quiet
ok "venv: $VENV_DIR ($(python --version))"

# ── Step 4: Python dependencies ──────────────────────────────────────────────
step "4/7 Installing requirements/research.txt"
[ -f "$REQUIREMENTS" ] || fail "Not found: $REQUIREMENTS"
pip install -r "$REQUIREMENTS" --quiet
ok "Dependencies installed:"
pip list | grep -E "SALib|ortools|google-cloud-storage|numpy|pandas|matplotlib|networkx|scipy|seaborn" \
    | awk '{printf "   %-30s %s\n", $1, $2}'

# Optional: BigQuery billing dependencies (check_billing.py real-cost mode)
if [ "$WITH_BILLING" -eq 1 ]; then
    step "4/7 (+) Installing requirements/billing.txt"
    [ -f "$BILLING_REQUIREMENTS" ] || fail "Not found: $BILLING_REQUIREMENTS"
    pip install -r "$BILLING_REQUIREMENTS" --quiet
    ok "Billing dependencies installed (check_billing.py BigQuery mode enabled)"
fi

# Optional: designer decision explorer dependencies (the /design process)
if [ "$WITH_DESIGN" -eq 1 ]; then
    step "4/7 (+) Installing requirements/design.txt"
    [ -f "$DESIGN_REQUIREMENTS" ] || fail "Not found: $DESIGN_REQUIREMENTS"
    pip install -r "$DESIGN_REQUIREMENTS" --quiet
    ok "Design dependencies installed (Designer_Decision_Explorer.ipynb enabled)"
fi

# ── Step 5: Install EnergyPlus 25.1.0 ────────────────────────────────────────
step "5/7 Installing EnergyPlus ${EPLUS_VERSION}"

if [ -f "${EPLUS_INSTALL}/pyenergyplus/api.py" ]; then
    ok "EnergyPlus already installed at ${EPLUS_INSTALL}"
else
    INSTALLER="$HOME/ep_installer.sh"
    echo "   Downloading EnergyPlus ${EPLUS_VERSION}..."
    wget -q --show-progress -O "$INSTALLER" "$EPLUS_URL" \
        || fail "Download failed"
    chmod +x "$INSTALLER"

    echo "   Installing (non-interactive mode)..."
    # Responds to 3 installer prompts:
    #   1. "Do you accept the license?" → y
    #   2. "Install directory [/usr/local/EnergyPlus-25-1-0]:" → Enter (default)
    #   3. "Symbolic link location:" → n
    printf 'y\n\nn\n' | sudo "$INSTALLER" \
        > /tmp/ep_install.log 2>&1 \
        || fail "Installation failed — see /tmp/ep_install.log"

    rm -f "$INSTALLER"
    ok "EnergyPlus ${EPLUS_VERSION} installed at ${EPLUS_INSTALL}"
fi

# ── Step 6: PYTHONPATH setup ─────────────────────────────────────────────────
step "6/7 Configuring PYTHONPATH"
PTH_DIR="$VENV_DIR/lib/python${PYTHON_VERSION}/site-packages"
mkdir -p "$PTH_DIR"
echo "$EPLUS_INSTALL" > "$PTH_DIR/energyplus.pth"
ok "energyplus.pth → $EPLUS_INSTALL"

# ── Step 7: Validation ───────────────────────────────────────────────────────
step "7/7 Validation"

ok "Python: $(python --version)"

EPLUS_VER=$(python -c "
import sys; sys.path.insert(0, '${EPLUS_INSTALL}')
from pyenergyplus.api import EnergyPlusAPI
print(EnergyPlusAPI().functional.ep_version())
" 2>&1)
echo "$EPLUS_VER" | grep -q "25.1" \
    && ok "EnergyPlus API: $EPLUS_VER" \
    || warn "EnergyPlus API: $EPLUS_VER"

python -c "
import numpy, pandas, SALib, ortools, networkx, scipy, seaborn, matplotlib
print('OK')
" > /dev/null 2>&1 \
    && ok "Imports: numpy pandas SALib ortools networkx scipy seaborn matplotlib" \
    || warn "Some imports failed — check research.txt"

GCS=$(python -c "
from google.cloud import storage
list(storage.Client().list_buckets(max_results=1))
print('OK')
" 2>&1)
echo "$GCS" | grep -q "OK" \
    && ok "GCS Workload Identity: authenticated" \
    || warn "GCS: $GCS"

ok "Disk: $(df -h / | awk 'NR==2{print $4}') free ($(df / | awk 'NR==2{print $5}') used)"

echo ""
echo "============================================================"
echo " Setup complete!"
echo "============================================================"
echo ""
echo " Activate venv:"
echo "   source $VENV_DIR/bin/activate"
echo ""
echo " Quick validation (~14s on e2-standard-8):"
echo "   cd $REPO_DIR"
echo "   python _GCP_VM_VERSION/run_simulation.py --mode validate"
echo ""
echo " Available modes:"
echo "   --mode validate           1 base simulation (~14s on e2-standard-8)"
echo "   --mode single             1 simulation with custom DPs"
echo "   --mode gsa                GSA Morris (--n-morris 20 for testing)"
echo "   --mode pareto             CP-SAT + EnergyPlus Pareto front"
echo "   --mode pareto-sequential  M3.8 Talami sequential block-propagation"
echo "   --mode exhaustive         M3.7 full factorial ground truth (192 sims)"
echo "   --mode calibrate          M3.1 LHS calibration for proxy training"
echo "============================================================"
