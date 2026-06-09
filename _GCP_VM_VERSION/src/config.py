"""
Centralized project configuration.
Override any value via environment variable before running.
"""

import os
from pathlib import Path


def _detect_eplus_dir() -> str:
    """
    Auto-detect EPLUS_DIR by environment:
      1. Env var EPLUS_DIR (highest priority)
      2. /usr/local/EnergyPlus-25-1-0 → GCP VM (setup_gcp_env.sh)
      3. /eplus          → Alternative installation
      4. ~/eplus         → Google Cloud Shell
      5. C:\\EnergyPlusV25-1-0 → Windows local (fallback)
    """
    if "EPLUS_DIR" in os.environ:
        return os.environ["EPLUS_DIR"]
    for candidate in [
        Path("/usr/local/EnergyPlus-25-1-0"),  # GCP VM (setup_gcp_env.sh)
        Path("/eplus"),  # alternative installation
        Path.home() / "eplus",  # Google Cloud Shell
    ]:
        if (candidate / "pyenergyplus").exists():
            return str(candidate)
    return r"C:\EnergyPlusV25-1-0"


EPLUS_DIR = _detect_eplus_dir()

# Building model selector (5zone / medium_office) — used by multizone modes
BUILDING_MODEL = os.environ.get("BUILDING_MODEL", "5zone")

# Schedule:Compact name for setpoint actuator in IDF (legacy single-zone mode)
CLG_SCHEDULE_NAME = os.environ.get("CLG_SCHEDULE_NAME", "Clg-SetP-Sch")

# Energy column in output CSV (instantaneous demand rate)
ENERGY_COLUMN = "Facility Total HVAC Electricity Demand Rate"

# ── Thermal conductivities of insulation materials (W/m·K) ────────────────────
# Used to convert R-value → thickness for Material objects.
K_IN02 = 0.043  # wall insulation (mineral wool / EPS typical)
K_IN46 = 0.023  # roof insulation (polyurethane / XPS typical)

# ── 5ZoneAirCooled — Small Office, 5 zones, Chicago TMY3 ─────────────────────
BUILDING_OBJECT_NAME = "Building"
OCCUPIED_ZONES = [
    "SPACE1-1",
    "SPACE2-1",
    "SPACE3-1",
    "SPACE4-1",
    "SPACE5-1",
]

# Zone → schedule name for Schedule:Compact actuators
ZONE_SCHEDULE_NAMES = {
    "SPACE1-1": "Clg-SetP-Sch-SPACE1-1",
    "SPACE2-1": "Clg-SetP-Sch-SPACE2-1",
    "SPACE3-1": "Clg-SetP-Sch-SPACE3-1",
    "SPACE4-1": "Clg-SetP-Sch-SPACE4-1",
    "SPACE5-1": "Clg-SetP-Sch-SPACE5-1",
}
ACTUATOR_COMPONENT_TYPE = "Schedule:Compact"
ACTUATOR_CONTROL_TYPE = "Schedule Value"
WALL_INSULATION_NAME = "IN02"
ROOF_INSULATION_NAME = "IN46"
INSULATION_MODE = "thickness"

# ── Design Parameter Domains ──────────────────────────────────────────────────
DP_DOMAINS = {
    "wall_r": (5, 25),    # Wall R-value × 0.1 → float
    "roof_r": (10, 40),   # Roof R-value × 0.1 → float
    "glass_u": (10, 30),  # Glazing U-value × 0.1 → float  (M1.6)
    "orientation": (0, 3),
    "setpoint": (23, 26),
}

ORIENTATION_MAP = {0: 0.0, 1: 90.0, 2: 180.0, 3: 270.0}  # index → degrees North

# ── Window Construction Catalog (M1.5) ────────────────────────────────────────
WINDOW_CATALOG = {
    "single_clear": "Sgl Grey 3mm",  # Existing in 5ZoneAirCooled
    "double_clear": "Dbl Clr 3mm/13mm Air",  # Existing in 5ZoneAirCooled
    "double_low_e": "Dbl LowE 3mm/13mm Air",  # Placeholder for future scaling
}
