"""
check_billing.py — GCP Billing Monitor
=======================================
Two modes (auto-detected, or forced via flags):

  --real      Query actual costs from BigQuery billing export.
  --estimate  Estimate costs from detected SKUs via gcloud CLI.

Without flags, tries BigQuery first and falls back to SKU estimates.

Usage:
    python _GCP_VM_VERSION/scripts/check_billing.py
    python _GCP_VM_VERSION/scripts/check_billing.py --days 7
    python _GCP_VM_VERSION/scripts/check_billing.py --real
    python _GCP_VM_VERSION/scripts/check_billing.py --estimate --today
"""

import argparse
import json
import math
import shutil
import subprocess
import sys
from datetime import datetime, timezone

# ── Configuration ─────────────────────────────────────────────────────────────
PROJECT_ID = "eplus-colab-cloud"
BILLING_DATA_PROJECT = "gen-lang-client-0464475716" # Altere para o ID do projeto onde  o dataset estiver configurado
BILLING_ACCOUNT = "018BDF-F25C35-6646B4"
BQ_DATASET = "faturamento_v1"
BQ_TABLE = f"gcp_billing_export_resource_v1_{BILLING_ACCOUNT.replace('-', '_')}"
CREDIT_CAP_BRL = 100.00
USD_TO_BRL = 5.70


# ── gcloud CLI Helpers ────────────────────────────────────────────────────────
def _gcloud(args: list, timeout: int = 20) -> dict | None:
    """Execute gcloud command and return JSON or None on error."""
    gcloud = shutil.which("gcloud") or "gcloud"
    cmd = [gcloud] + args + ["--format=json"]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=(sys.platform == "win32"),
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except Exception:
        return None


def get_billing_status(project_id: str) -> dict:
    """Return billing status of project via gcloud CLI."""
    info = _gcloud(["beta", "billing", "projects", "describe", project_id])
    if not info:
        return {"error": "gcloud failed"}
    return {
        "active": info.get("billingEnabled", False),
        "account": info.get("billingAccountName", "N/A").split("/")[-1],
    }


# ── Real costs via BigQuery billing export ────────────────────────────────────
def query_actual_costs(days: int = 30) -> "pd.DataFrame | None":
    """
    Query actual costs from the GCP billing export BigQuery table.
    Returns a DataFrame or None if unavailable.
    Requires: pip install google-cloud-bigquery pandas
    """
    try:
        import io
        import sys as _sys

        import google.auth
        from google.api_core.exceptions import Forbidden, NotFound
        from google.cloud import bigquery

        # Ensure UTF-8 stdout on Windows
        _sys.stdout = io.TextIOWrapper(
            _sys.stdout.buffer, encoding="utf-8", errors="replace"
        )

        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        client = bigquery.Client(project=PROJECT_ID, credentials=credentials)

        query = f"""
        SELECT
          service.description AS service_name,
          SUM(cost)           AS gross_cost,
          SUM((SELECT SUM(amount) FROM UNNEST(credits))) AS credits,
          SUM(cost + (SELECT IFNULL(SUM(amount), 0) FROM UNNEST(credits))) AS net_cost
        FROM `{BILLING_DATA_PROJECT}.{BQ_DATASET}.{BQ_TABLE}`
        WHERE _PARTITIONDATE >= DATE_SUB(CURRENT_DATE(), INTERVAL {days} DAY)
        GROUP BY 1
        ORDER BY net_cost DESC
        """

        df = client.query(query).to_dataframe()
        return df

    except ImportError as e:
        print(f"\n   [!] Missing dependency for BigQuery: {e}")
        return None
    except NotFound as e:
        print(f"\n   [!] BigQuery table not found: {e}")
        return None
    except Forbidden as e:
        if "OAuth scope" in str(e):
            print("\n   [!] ADC missing BigQuery scope. Run:")
            print(
                "       gcloud auth application-default login --scopes=https://www.googleapis.com/auth/cloud-platform"
            )
        else:
            print(f"\n   [!] BigQuery access denied: {e}")
        return None
    except Exception as e:
        print(f"\n   [!] BigQuery query failed: {type(e).__name__} - {e}")
        return None


# ── Estimated costs via SKU prices ────────────────────────────────────────────
def get_cost_skus(project_id: str, days: int = 1) -> list[dict]:
    """
    List active resources and estimate cost based on Compute Engine SKUs.
    Reliable even without BigQuery export configured.
    """
    costs = []

    instances = _gcloud(
        [
            "compute",
            "instances",
            "list",
            "--project",
            project_id,
            "--filter",
            "status:(RUNNING OR TERMINATED)",
        ]
    )

    if instances:
        MACHINE_PRICES = {
            "e2-medium": 0.0335,
            "e2-standard-4": 0.134,
            "n4-standard-4": 0.189,
            "c2-standard-4": 0.209,
        }
        for vm in instances:
            machine = vm.get("machineType", "").split("/")[-1]
            zone = vm.get("zone", "").split("/")[-1]
            status = vm.get("status", "")
            disk_gb = sum(int(d.get("diskSizeGb", 0)) for d in vm.get("disks", []))
            costs.append(
                {
                    "resource": f"VM: {vm.get('name')} ({machine})",
                    "zone": zone,
                    "status": status,
                    "price_hr_usd": MACHINE_PRICES.get(machine, 0.05),
                    "disk_gb": disk_gb,
                    "disk_cost_day": round(disk_gb * 0.040 / 30, 4),
                }
            )

    buckets = _gcloud(["storage", "buckets", "list", "--project", project_id])
    if buckets:
        for b in buckets:
            costs.append(
                {
                    "resource": f"GCS: {b.get('name', 'N/A')}",
                    "zone": "global",
                    "status": "ACTIVE",
                    "price_hr_usd": 0.0,
                    "disk_gb": 0,
                    "disk_cost_day": 0.0,
                    "note": "$0.020/GB/month (volume depends on usage)",
                }
            )

    return costs


# ── Display helpers ───────────────────────────────────────────────────────────
def print_real_costs(df, days: int) -> None:
    """Print actual costs table from BigQuery."""
    print(f"\n   Source: BigQuery billing export (last {days} day(s))")

    if df is None:
        print("   [!] BigQuery unavailable — table not found or missing dependencies.")
        return

    if df.empty:
        print("   Table found, but no records for the selected period yet.")
        return

    print()
    header = f"   {'Service':<40} {'Gross (USD)':>12} {'Credits (USD)':>14} {'Net (USD)':>12}"
    print(header)
    print("   " + "-" * (len(header) - 3))
    for _, row in df.iterrows():
        credits = row["credits"]
        if credits is None or (isinstance(credits, float) and math.isnan(credits)):
            credits = 0.0
        print(
            f"   {row['service_name']:<40} "
            f"{row['gross_cost']:>12.4f} "
            f"{credits:>14.4f} "
            f"{row['net_cost']:>12.4f}"
        )

    total_net = df["net_cost"].sum()
    total_brl = total_net * USD_TO_BRL
    print("   " + "-" * (len(header) - 3))
    print(f"\n   Total net cost : USD {total_net:.4f}  ~  R$ {total_brl:.2f}")
    print(f"   Credit cap     : R$ {CREDIT_CAP_BRL:.2f}")
    print(f"   USD->BRL rate  : {USD_TO_BRL}")


def print_estimated_costs(project_id: str, days: int) -> None:
    """Print estimated costs from detected SKUs."""
    print(f"\n   Source: SKU estimates via gcloud (last {days} day(s))")
    costs = get_cost_skus(project_id, days)
    total_usd = 0.0

    print("\n   Detected resources:")
    for c in costs:
        note = c.get("note", "")
        vm_cost_day = c["price_hr_usd"] * 24 * days
        total_resource = vm_cost_day + c["disk_cost_day"] * days

        if c["status"] == "TERMINATED":
            total_resource = c["disk_cost_day"] * days
            print(f"   • {c['resource']}")
            print(f"     Status: STOPPED | Disk: {c['disk_gb']}GB")
            print(f"     Disk cost ({days}d): ~USD {total_resource:.4f}")
        elif c["price_hr_usd"] > 0:
            print(f"   • {c['resource']}")
            print(f"     Status: {c['status']} | Disk: {c['disk_gb']}GB")
            print(
                f"     VM cost ({days}d @ USD {c['price_hr_usd']}/hr): ~USD {vm_cost_day:.4f}"
            )
            print(f"     Disk cost ({days}d): ~USD {c['disk_cost_day'] * days:.4f}")
            print(f"     Subtotal: ~USD {total_resource:.4f}")
        else:
            print(f"   • {c['resource']} — {note}")

        total_usd += total_resource

    total_brl = total_usd * USD_TO_BRL
    print(f"\n   Total estimated : USD {total_usd:.4f}  ~  R$ {total_brl:.2f}")
    print(f"   Credit cap      : R$ {CREDIT_CAP_BRL:.2f}")
    print(f"   USD->BRL rate   : {USD_TO_BRL}")
    print()
    print("   [!] Values are ESTIMATES based on detected SKUs.")
    print(
        f"   For real costs: https://console.cloud.google.com/billing/{BILLING_ACCOUNT}/reports"
    )


# ── CSV export / stage-out ────────────────────────────────────────────────────
def export_billing_csv(df, days: int, path: str) -> str:
    """Write the cost table to a CSV file (BigQuery rows or SKU estimates)."""
    import csv as _csv

    rows = []
    if df is not None and not df.empty:
        source = "bigquery"
        for _, r in df.iterrows():
            credits = r["credits"]
            if credits is None or (isinstance(credits, float) and math.isnan(credits)):
                credits = 0.0
            rows.append(
                {
                    "service": r["service_name"],
                    "gross_usd": round(float(r["gross_cost"]), 4),
                    "credits_usd": round(float(credits), 4),
                    "net_usd": round(float(r["net_cost"]), 4),
                }
            )
    else:
        source = "sku_estimate"
        for c in get_cost_skus(PROJECT_ID, days):
            if c["status"] == "TERMINATED":
                subtotal = c["disk_cost_day"] * days
            else:
                subtotal = c["price_hr_usd"] * 24 * days + c["disk_cost_day"] * days
            rows.append(
                {
                    "service": c["resource"],
                    "gross_usd": round(subtotal, 4),
                    "credits_usd": "",
                    "net_usd": "",
                }
            )

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = _csv.DictWriter(
            f, fieldnames=["service", "gross_usd", "credits_usd", "net_usd"]
        )
        writer.writeheader()
        writer.writerows(rows)
    print(f"\n   CSV ({source}, {len(rows)} rows) → {path}")
    return path


def stage_billing_csv(path: str, ts: str) -> None:
    """Upload the billing CSV to gs://eplus-colab-cloud-data/billing/."""
    try:
        from google.cloud import storage

        name = f"gcp_vm_billing_{ts}.csv"
        client = storage.Client(project=PROJECT_ID)
        client.bucket("eplus-colab-cloud-data").blob(
            f"billing/{name}"
        ).upload_from_filename(path)
        print(f"   Staged → gs://eplus-colab-cloud-data/billing/{name}")
    except Exception as e:
        print(f"   [!] Stage-out failed: {type(e).__name__} — {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="GCP Billing Monitor")
    parser.add_argument(
        "--days", type=int, default=30, help="Days window (default: 30)"
    )
    parser.add_argument("--today", action="store_true", help="Use a 1-day window")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--real", action="store_true", help="Force BigQuery mode")
    mode.add_argument("--estimate", action="store_true", help="Force SKU estimate mode")
    parser.add_argument(
        "--csv",
        nargs="?",
        const="",
        default=None,
        metavar="PATH",
        help="Write the cost table as CSV (default: /tmp/gcp_vm_billing_<ts>.csv)",
    )
    parser.add_argument(
        "--stage-out",
        action="store_true",
        help="Upload the CSV to gs://eplus-colab-cloud-data/billing/ (implies --csv)",
    )
    args = parser.parse_args()

    days = 1 if args.today else args.days
    now = datetime.now(timezone.utc)

    print("=" * 60)
    print(f"GCP BILLING MONITOR — {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"   Project : {PROJECT_ID}")
    print(f"   Window  : last {days} day(s)")
    print("=" * 60)

    # Billing status
    status = get_billing_status(PROJECT_ID)
    if "error" not in status:
        flag = "ACTIVE" if status["active"] else "INACTIVE"
        print(f"\n   Billing: {flag} | Account: {status['account']}")

    # Cost report
    df = None
    if args.estimate:
        print_estimated_costs(PROJECT_ID, days)
    elif args.real:
        df = query_actual_costs(days)
        print_real_costs(df, days)
    else:
        # Auto: try BigQuery, fall back to estimates
        df = query_actual_costs(days)
        if df is not None:
            print_real_costs(df, days)
        else:
            print("\n   [i] BigQuery unavailable — falling back to SKU estimates.")
            print_estimated_costs(PROJECT_ID, days)

    # Optional CSV export / stage-out
    if args.csv is not None or args.stage_out:
        ts = now.strftime("%Y%m%d_%H%M%S")
        csv_path = args.csv if args.csv else f"/tmp/gcp_vm_billing_{ts}.csv"
        export_billing_csv(df, days, csv_path)
        if args.stage_out:
            stage_billing_csv(csv_path, ts)

    print("\n" + "=" * 60)


if __name__ == "__main__":
    main()
