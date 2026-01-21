# MagBank Monitor

**MagBank Monitor** is a precision CLI telemetry dashboard and battery capacity tester designed for Linux. It bridges the gap between hardware USB testers and software analysis, providing a real-time "Science Lab" environment for evaluating power bank health, charging protocols, and energy efficiency.

Designed specifically for the **FNIRSI FNB58** USB Tester.

![Dashboard Preview](https://via.placeholder.com/800x400?text=MagBank+Monitor+CLI+Dashboard)

## Key Features

*   **Remote Telemetry Dashboard:** Mirrors the FNB58's internal measurements to your PC screen via HID, allowing for monitoring even when the physical device screen is obscured.
    *   **VBUS / IBUS / PBUS:** Real-time Voltage, Current, and Power monitoring.
    *   **Protocol Analysis:** Displays D+/D- voltages and inferred charging protocols (PD, QC, DCP, Apple 2.4A).
*   **Capacity & Health Testing:**
    *   **High-Precision Coulomb Counting:** Processes all 4 samples per packet (100 Hz) for accurate mAh and Wh accumulation.
    *   **Session Management:** Track energy throughput with session timer and resettable counters.
*   **Live Statistics:**
    *   **Min / Avg / Max:** Real-time statistics for voltage and current throughout the session.
    *   **Temperature Monitoring:** EMA-smoothed temperature display.
*   **Interactive Controls:**
    *   **[r] Reset:** Reset session counters, statistics, and timer mid-session.
    *   **[q] Quit:** Clean exit with proper USB cleanup.
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

## Dashboard Layout

The interface is divided into two logical sectors:

*   **EXTERNAL LOAD (Top):** Displays high-frequency telemetry from the FNB58:
    *   Row 1: Voltage (VBUS) / Current (IBUS) / Power (PBUS)
    *   Row 2: Energy (Wh) / Capacity (mAh) / Temperature
    *   Row 3: Data Lines (D+/D-) / Protocol / Session Timer
    *   Row 4: Voltage Stats (min/avg/max) / Current Stats (min/avg/max) / Controls
*   **SYSTEM SENSORS (Bottom):** Displays the host computer's internal power status (Battery %, Charging State), provided by the Linux kernel (`/sys/class/power_supply`).

### Keyboard Controls

| Key | Action |
|-----|--------|
| `r` | Reset session (clears mAh, Wh, statistics, and timer) |
| `q` | Quit the monitor |
| `Ctrl+C` | Force quit |

## Technical Details

*   **Protocol:** Custom HID implementation over Interface 3, Interrupt Endpoints `0x83` (IN) and `0x03` (OUT).
*   **Data Rate:** 100 Hz sampling (4 samples per 64-byte packet), 1 Hz display refresh.
*   **Packet Structure:** Each packet contains 4 samples of 15 bytes each:
    *   Bytes 0-3: Voltage (32-bit LE, /100000 for V)
    *   Bytes 4-7: Current (32-bit LE, /100000 for A)
    *   Bytes 8-9: D+ voltage (16-bit LE, /1000 for V)
    *   Bytes 10-11: D- voltage (16-bit LE, /1000 for V)
    *   Bytes 13-14: Temperature (16-bit LE, /10 for C)
*   **Logging:** All session data is automatically appended to `magbank_history.jsonl` for offline analysis.

## 🤝 Contributing

Contributions are welcome! Please submit Pull Requests for:
*   Support for additional USB Testers (Power-Z, Witt).
*   Enhanced graphing or plotting tools for the JSONL history.
*   Database integration (SQLite/InfluxDB).

## 📜 License

MIT License. See `LICENSE` for details.
