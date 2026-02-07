# MagBank Monitor

**MagBank Monitor** is a precision CLI telemetry dashboard and battery capacity tester designed for Linux. It bridges the gap between hardware USB testers and software analysis, providing a real-time "Science Lab" environment for evaluating power bank health, charging protocols, and energy efficiency.

Designed for the **FNIRSI FNB58** USB Tester and **Anker 575 USB-C Docking Station**.

![Dashboard Preview](https://via.placeholder.com/800x400?text=MagBank+Monitor+CLI+Dashboard)

## Key Features

*   **Remote Telemetry Dashboard:** Mirrors the FNB58's internal measurements to your PC screen via HID, allowing for monitoring even when the physical device screen is obscured.
    *   **VBUS / IBUS / PBUS:** Real-time Voltage, Current, and Power monitoring.
    *   **Protocol Analysis:** Displays D+/D- voltages and inferred charging protocols (PD, QC, DCP, Apple 2.4A).
*   **Anker 575 Dock Monitoring:**
    *   **Power Delivery (Inferred):** Reads laptop battery and AC status to infer dock charging state — "Charging via Dock (X.XW)", "Maintaining (Dock Connected)", etc.
    *   **Battery Health:** Displays capacity %, energy (now/full Wh), health % (energy_full / design), and cycle count.
    *   **Gigabit Ethernet (RTL8153):** Auto-detects the dock's Realtek RTL8153 adapter by USB VID:PID — no hardcoded interface names. Shows link state, speed/duplex, MAC, MTU, real-time RX/TX rates (bytes/s + pps), cumulative totals, packet counts, error/drop counters, and carrier change events.
*   **Capacity & Health Testing:**
    *   **High-Precision Coulomb Counting:** Processes all 4 samples per packet (100 Hz) for accurate mAh and Wh accumulation.
    *   **Auto-Stop on Charge Complete:** Detects when a power bank finishes charging (power drops below 1W for 10s after active charging) and freezes mAh/Wh counters automatically — no more inflated readings from trickle current. Dashboard turns green with "COMPLETE" and session duration. Auto-resumes if a new bank is plugged in.
    *   **Session Management:** Track energy throughput with session timer and resettable counters.
*   **Live Statistics:**
    *   **Min / Avg / Max:** Real-time statistics for voltage and current throughout the session.
    *   **Temperature Monitoring:** EMA-smoothed temperature display.
*   **Interactive Controls:**
    *   **[r] Reset:** Reset session counters, statistics, and timer mid-session.
    *   **[q] Quit:** Clean exit with proper USB cleanup.
*   **Hybrid Monitoring:** Simultaneously monitors external USB loads, dock peripherals, *and* host system power sensors in a unified three-panel view.
*   **Zero-Driver Setup:** Uses pure Python `pyusb` with custom `udev` rules—no kernel modules required. Dock and ethernet monitoring uses only sysfs (stdlib `os`/`pathlib`).

## Hardware

*   **FNIRSI FNB58** (or FNB48S) USB Tester.
*   **Anker 575 USB-C Docking Station** (A83B61A1) — optional, auto-detected.
*   **Cabling:**
    *   USB-C to USB-C (for the power path).
    *   Micro-USB to USB-A/C (for the FNB58 PC data link).
*   **Host OS:** Linux (Tested on Kernel 6.x).

## Installation

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

## Usage

1.  **Connect the Hardware:**
    *   **Source:** Charger -> FNB58 `TYPE-C IN`
    *   **Load:** FNB58 `TYPE-C OUT` -> Power Bank / Phone
    *   **Data:** FNB58 `PC Port` -> Computer USB
    *   **Dock:** Anker 575 connected via USB-C (detected automatically)

2.  **Run the Monitor:**
    ```bash
    source venv/bin/activate
    python3 monitor.py
    ```

3.  **Simulation Mode (No Hardware):**
    To test the UI layout without an FNB58 connected (dock/ethernet data is always live from sysfs). Simulates a full charge cycle: 15s active charging, ramp-down, then charge-complete trigger at ~30s.
    ```bash
    python3 monitor.py --simulate
    ```

## Dashboard Layout

The interface is divided into three panels:

*   **EXTERNAL LOAD — FNB58 (Top):** High-frequency telemetry from the FNB58:
    *   Row 1: Voltage (VBUS) / Current (IBUS) / Power (PBUS)
    *   Row 2: Energy (Wh) / Capacity (mAh) / Temperature — panels show charge state: dim "Waiting..." before charging, yellow during active charge, "Settling..." during power drop detection, green "COMPLETE in MM:SS" when done
    *   Row 3: Data Lines (D+/D-) / Protocol / Session Timer
    *   Row 4: Voltage Stats (min/avg/max) / Current Stats (min/avg/max) / Controls
*   **ANKER 575 USB-C DOCK (Middle):** Split horizontally:
    *   **Power Delivery (Left):** Inferred charging state, AC mains, battery %, power draw, voltage, energy (now/full Wh), health %, cycle count.
    *   **Gigabit Ethernet (Right):** 3x3 grid — link status, speed/duplex, interface/MAC/MTU, RX rate, TX rate, cumulative totals, packet counts, errors/drops, carrier changes.
*   **SYSTEM SENSORS (Bottom):** Host computer's internal power status (Battery %, Charging State) from `/sys/class/power_supply`.

### Keyboard Controls

| Key | Action |
|-----|--------|
| `r` | Reset session (clears mAh, Wh, statistics, and timer) |
| `q` | Quit the monitor |
| `Ctrl+C` | Force quit |

## Technical Details

### FNB58 Protocol
*   **Interface:** Custom HID over Interface 3, Interrupt Endpoints `0x83` (IN) and `0x03` (OUT).
*   **Data Rate:** 100 Hz sampling (4 samples per 64-byte packet), 1 Hz display refresh.
*   **Packet Structure:** Each packet contains 4 samples of 15 bytes each:
    *   Bytes 0-3: Voltage (32-bit LE, /100000 for V)
    *   Bytes 4-7: Current (32-bit LE, /100000 for A)
    *   Bytes 8-9: D+ voltage (16-bit LE, /1000 for V)
    *   Bytes 10-11: D- voltage (16-bit LE, /1000 for V)
    *   Bytes 13-14: Temperature (16-bit LE, /10 for C)

### Anker 575 Dock
*   **Detection:** Scans `/sys/bus/usb/devices/` for HID VID `291a`, PIDs `03b6`/`83b6`.
*   **Power:** The Anker 575 does not expose USB-PD data through sysfs. Power delivery status is inferred from `/sys/class/power_supply/BAT0/` (battery state, power_now, energy) and `/sys/class/power_supply/AC/` (mains online).
*   **Ethernet:** Auto-detects the RTL8153 Gigabit adapter by walking `/sys/class/net/*/device` USB tree for VID `0bda` PID `8153`. Computes real-time byte and packet rates via counter deltas each refresh cycle. Handles cable disconnection gracefully (re-detects on sysfs failure).

### Logging
*   All session data is automatically appended to `magbank_history.jsonl`.
*   Each JSONL record contains `fnb58`, `system`, and `anker_dock` (with `power` and `ethernet` sub-objects) keys.

## Contributing

Contributions are welcome! Please submit Pull Requests for:
*   Support for additional USB Testers (Power-Z, Witt).
*   Enhanced graphing or plotting tools for the JSONL history.
*   Database integration (SQLite/InfluxDB).
*   Anker 575 HID protocol reverse-engineering for direct PD data.

## License

MIT License. See `LICENSE` for details.
