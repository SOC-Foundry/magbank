# MagBank Monitor

**MagBank Monitor** is a precision CLI telemetry dashboard and battery capacity tester designed for Linux. It bridges the gap between hardware USB testers and software analysis, providing a real-time "Science Lab" environment for evaluating power bank health, charging protocols, and energy efficiency.

Designed specifically for the **FNIRSI FNB58** USB Tester.

![Dashboard Preview](https://via.placeholder.com/800x400?text=MagBank+Monitor+CLI+Dashboard)

## 🚀 Key Features

*   **Remote Telemetry Dashboard:** Mirrors the FNB58's internal measurements to your PC screen via HID, allowing for monitoring even when the physical device screen is obscured.
    *   **VBUS / IBUS / PBUS:** Real-time Voltage, Current, and Power monitoring.
    *   **Protocol Analysis:** Displays D+/D- voltages and inferred charging protocols (PD, QC, etc.).
*   **Capacity & Health Testing:**
    *   **Software Integration:** Performs high-precision Coulomb counting (mAh and Wh accumulation) on the host side.
    *   **Session Persistence:** Tracks total energy throughput for a charging session to verify manufacturer capacity claims.
*   **Hybrid Monitoring:** Simultaneously monitors external USB loads *and* host system power sensors (internal laptop battery, AC status) in a unified split-screen view.
*   **Zero-Driver Setup:** Uses pure Python `pyusb` with custom `udev` rules—no kernel modules required.

## 🛠 Hardware Requirements

*   **FNIRSI FNB58** (or FNB48S) USB Tester.
*   **Cabling:**
    *   USB-C to USB-C (for the power path).
    *   Micro-USB to USB-A/C (for the PC data link).
*   **Host OS:** Linux (Tested on Kernel 6.x).

## 📦 Installation

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/yourusername/magbank-monitor.git
    cd magbank-monitor/magbank-monitor
    ```

2.  **Set up the environment:**
    ```bash
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
    ```

3.  **Configure Permissions (Linux):**
    Install the udev rule to allow non-root access to the HID interface.
    ```bash
    sudo ./setup_udev.sh
    ```
    *Unplug and replug your FNB58 after running this.*

## 🖥 Usage

1.  **Connect the Hardware:**
    *   **Source:** Charger -> FNB58 `TYPE-C IN`
    *   **Load:** FNB58 `TYPE-C OUT` -> Power Bank / Phone
    *   **Data:** FNB58 `PC Port` -> Computer USB

2.  **Run the Monitor:**
    ```bash
    source venv/bin/activate
    python3 monitor.py
    ```

3.  **Simulation Mode (No Hardware):**
    To test the UI layout without a device connected:
    ```bash
    python3 monitor.py --simulate
    ```

## 📊 Dashboard Layout

The interface is divided into two logical sectors:

*   **EXTERNAL LOAD (Top):** Displays high-frequency telemetry from the FNB58. Use this to monitor the specific device you are testing.
*   **SYSTEM SENSORS (Bottom):** Displays the host computer's internal power status (Battery %, Charging State), provided by the Linux kernel (`/sys/class/power_supply`).

## 🔧 Technical Details

*   **Protocol:** Custom HID implementation over Interface 3, Interrupt Endpoints `0x83` (IN) and `0x03` (OUT).
*   **Data Rate:** ~10-20Hz polling rate (configurable).
*   **Logging:** All session data is automatically appended to `magbank_history.jsonl` for offline analysis.

## 🤝 Contributing

Contributions are welcome! Please submit Pull Requests for:
*   Support for additional USB Testers (Power-Z, Witt).
*   Enhanced graphing or plotting tools for the JSONL history.
*   Database integration (SQLite/InfluxDB).

## 📜 License

MIT License. See `LICENSE` for details.
