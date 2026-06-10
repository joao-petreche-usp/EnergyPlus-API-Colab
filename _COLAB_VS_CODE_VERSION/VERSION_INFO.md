### Colab and VS Code Hybrid Version

A hybrid workflow combining the local development environment (VS Code) with the remote processing capabilities of Google Colab. The frontend runs in VS Code (IntelliSense, Copilot, local file editing); the backend executes on a Colab Linux runtime (EnergyPlus installation, GCS access). No repository clone required.

#### Prerequisites
*   VS Code with the [Google Colab extension](https://marketplace.visualstudio.com/items?itemName=google.colab) installed.
*   An active Colab runtime connection (**Connect to Colab** in VS Code).
*   Prior login at [colab.research.google.com](https://colab.research.google.com) before connecting via the extension.
*   Access to the GCS bucket `eplus-colab-cloud-data` with inputs under `models/` and `weather/`.

#### Primary Files
*   **`EnergyPlus_VS_Code_Colab.ipynb`**: Notebook optimized for the VS Code + Colab extension architecture. Updated February 2026 to the new bucket structure. Implements a 10-cell pipeline:
    1. GCP authentication via `gcloud` (interactive URL flow adapted for VS Code input modal).
    2. Bucket and input file configuration (`models/`, `weather/`, `resultados/`).
    3. EnergyPlus v25.1.0 installation on the remote Colab Linux VM.
    4. Stage-In: download IDF and EPW from GCS.
    5. Simulation via `pyenergyplus` Python API.
    6. Stage-Out: upload outputs to `resultados/simulacao_vscode_{timestamp}/`.
    7. Validation via HTML report.
    8. Administrative information.
    9–10. Utility cells for exploring and managing files in the bucket.

#### Execution Instructions
1. Install the **Google Colab** extension in VS Code.
2. Log in at [colab.research.google.com](https://colab.research.google.com) in your browser.
3. In VS Code, open `EnergyPlus_VS_Code_Colab.ipynb` and connect to a Colab Runtime.
4. Execute the cells sequentially (1 → 8). Do **not** run with a local Python kernel.

> **Technical Note:** This module illustrates a hybrid development architecture, offering an optimal authoring experience (VS Code) connected directly to the cloud computing power of Google Colab. It highlights the framework's flexibility in bridging local development tools with scalable cloud infrastructure.
