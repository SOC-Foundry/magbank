import os
import time
import json
import datetime
import random
import argparse
import sys
import struct
import select
import termios
import tty
from pathlib import Path
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.layout import Layout
from rich.text import Text
from rich import box

# Try to import pyusb and crc
try:
    import usb.core
    import usb.util
    USB_AVAILABLE = True
except ImportError:
    USB_AVAILABLE = False

try:
    import crc
    CRC_AVAILABLE = True
except ImportError:
    CRC_AVAILABLE = False

# Configuration
LOG_FILE = "magbank_history.jsonl"
SYS_CLASS_DIR = "/sys/class/power_supply"
REFRESH_RATE = 1  # seconds

# FNB58 Constants
VID_FNB58 = 0x2E3C
PID_FNB58 = 0x5558

console = Console()

# Anker 575 Dock Constants
ANKER_HID_VID = "291a"
ANKER_HID_PIDS = {"03b6", "83b6"}
RTL8153_VID = "0bda"
RTL8153_PID = "8153"
SYS_CLASS_NET = "/sys/class/net"
SYS_BUS_USB = "/sys/bus/usb/devices"
SYS_PS_BAT0 = "/sys/class/power_supply/BAT0"
SYS_PS_AC = "/sys/class/power_supply/AC"


def read_sysfs_file(*path_parts):
    """Read a sysfs file, return stripped string or None on failure."""
    try:
        with open(os.path.join(*path_parts), 'r', errors='replace') as f:
            return f.read().strip()
    except (OSError, FileNotFoundError):
        return None


def read_sysfs_int(*path_parts):
    """Read a sysfs file as int, return None on failure."""
    val = read_sysfs_file(*path_parts)
    if val is None:
        return None
    try:
        return int(val)
    except ValueError:
        return None


def format_bytes(n):
    """Format byte count to human-readable string."""
    if n is None:
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024.0:
            if unit == "B":
                return f"{n:.0f} {unit}"
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


def format_rate(bps):
    """Format bytes/sec to human-readable rate."""
    if bps is None or bps < 0:
        return "-"
    return format_bytes(bps) + "/s"


# --- FNB58 Protocol Implementation ---
class FNB58Device:
    def __init__(self, simulate=False):
        self.simulate = simulate
        self.device = None
        self.ep_in = None
        self.ep_out = None
        self.connected = False
        self.data = {
            "voltage_v": 0.0,
            "current_a": 0.0,
            "power_w": 0.0,
            "energy_wh": 0.0,
            "capacity_mah": 0.0,
            "temp_c": 0.0,
            "protocol": "Unknown",
            "dp_v": 0.0,
            "dm_v": 0.0
        }

        # Simulation state
        self.sim_capacity_wh = 0.0
        self.sim_capacity_mah = 0.0
        self.sim_start_time = time.time()

        # Integration state
        self.last_read_time = time.time()

        # Temperature smoothing (EMA)
        self.temp_ema = None
        self.temp_alpha = 0.9  # Smoothing factor (0.9 = heavy smoothing)

        # Sample timing for accurate integration
        self.sample_interval = 0.01  # 10ms per sample (100 Hz)

        # Session tracking
        self.session_start_time = time.time()

        # Statistics tracking
        self.stats = {
            "voltage_min": float('inf'),
            "voltage_max": float('-inf'),
            "voltage_sum": 0.0,
            "current_min": float('inf'),
            "current_max": float('-inf'),
            "current_sum": 0.0,
            "sample_count": 0
        }

    def reset_session(self):
        """Reset session counters and statistics"""
        self.data["energy_wh"] = 0.0
        self.data["capacity_mah"] = 0.0
        self.session_start_time = time.time()
        self.temp_ema = None

        # Reset simulation counters too
        self.sim_capacity_wh = 0.0
        self.sim_capacity_mah = 0.0
        self.sim_start_time = time.time()

        # Reset statistics
        self.stats = {
            "voltage_min": float('inf'),
            "voltage_max": float('-inf'),
            "voltage_sum": 0.0,
            "current_min": float('inf'),
            "current_max": float('-inf'),
            "current_sum": 0.0,
            "sample_count": 0
        }

    def get_session_duration(self):
        """Get formatted session duration"""
        elapsed = time.time() - self.session_start_time
        hours, remainder = divmod(int(elapsed), 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours > 0:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        else:
            return f"{minutes:02d}:{seconds:02d}"

    def get_stats_display(self):
        """Get formatted statistics for display"""
        s = self.stats
        if s["sample_count"] == 0:
            return {
                "voltage": "- / - / -",
                "current": "- / - / -"
            }

        v_avg = s["voltage_sum"] / s["sample_count"]
        c_avg = s["current_sum"] / s["sample_count"]

        # Handle inf values for display
        v_min = s["voltage_min"] if s["voltage_min"] != float('inf') else 0
        v_max = s["voltage_max"] if s["voltage_max"] != float('-inf') else 0
        c_min = s["current_min"] if s["current_min"] != float('inf') else 0
        c_max = s["current_max"] if s["current_max"] != float('-inf') else 0

        return {
            "voltage": f"{v_min:.2f} / {v_avg:.2f} / {v_max:.2f}",
            "current": f"{c_min:.3f} / {c_avg:.3f} / {c_max:.3f}"
        }

    def _update_stats(self, voltage, current):
        """Update running statistics"""
        self.stats["voltage_min"] = min(self.stats["voltage_min"], voltage)
        self.stats["voltage_max"] = max(self.stats["voltage_max"], voltage)
        self.stats["voltage_sum"] += voltage
        self.stats["current_min"] = min(self.stats["current_min"], current)
        self.stats["current_max"] = max(self.stats["current_max"], current)
        self.stats["current_sum"] += current
        self.stats["sample_count"] += 1

    def connect(self):
        if self.simulate:
            self.connected = True
            console.print("[bold green]FNB58 Simulated: Connected[/bold green]")
            return True
        
        if not USB_AVAILABLE:
            console.print("[bold red]pyusb not installed or not available. Cannot connect to FNB58.[/bold red]")
            return False

        try:
            intf_number = 3 # FNB58 data is on Interface 3 (HID)

            self.device = usb.core.find(idVendor=VID_FNB58, idProduct=PID_FNB58)
            if self.device is None:
                self.connected = False
                console.print("[bold yellow]FNB58: Device not found. Ensure it's connected via PC port.[/bold yellow]")
                return False
            
            console.print("[bold green]FNB58: Device found.[/bold green]")

            # Detach kernel drivers from ALL interfaces (required for clean access)
            for cfg in self.device:
                for intf in cfg:
                    intf_num = intf.bInterfaceNumber
                    if self.device.is_kernel_driver_active(intf_num):
                        try:
                            console.print(f"[yellow]FNB58: Detaching kernel driver from Interface {intf_num}...[/yellow]")
                            self.device.detach_kernel_driver(intf_num)
                        except usb.core.USBError as e:
                            console.print(f"[dim]FNB58: Interface {intf_num} detach warning: {e}[/dim]")

            # Set configuration
            try:
                self.device.set_configuration()
                console.print("[green]FNB58: Configuration set.[/green]")
            except usb.core.USBError as e:
                console.print(f"[yellow]FNB58: Config set skipped/failed: {e}[/yellow]")
            
            # Claim the specific interface
            usb.util.claim_interface(self.device, intf_number)
            console.print(f"[green]FNB58: Interface {intf_number} claimed.[/green]")

            cfg = self.device.get_active_configuration()
            intf = cfg[(intf_number,0)] # Access Interface 3, Alt Setting 0
            
            # Find Interrupt endpoints for Interface 3 by hardcoding known addresses
            self.ep_out = usb.util.find_descriptor(intf, bEndpointAddress=0x03) # FNB58 OUT Endpoint
            self.ep_in = usb.util.find_descriptor(intf, bEndpointAddress=0x83)  # FNB58 IN Endpoint
            
            if self.ep_in and self.ep_out:
                console.print(f"[green]FNB58: Endpoints found. OUT: {hex(self.ep_out.bEndpointAddress)} IN: {hex(self.ep_in.bEndpointAddress)}[/green]")
                # --- FNB58 Handshake Sequence (Call and Response) ---
                # 1. Send Init 1
                cmd_init1 = b"\xaa\x81" + b"\x00" * 61 + b"\x8e"
                console.print("[dim]FNB58: Sending Init 1...[/dim]")
                self.ep_out.write(cmd_init1, timeout=1000)
                time.sleep(0.05)
                # Read response for Init 1 (important to clear buffer)
                resp1 = self.device.read(self.ep_in.bEndpointAddress, 64, timeout=1000)
                console.print(f"[dim]FNB58: Init 1 Response: {list(resp1[:4])}...[/dim]")
                
                # 2. Send Init 2
                cmd_init2 = b"\xaa\x82" + b"\x00" * 61 + b"\x96"
                console.print("[dim]FNB58: Sending Init 2...[/dim]")
                self.ep_out.write(cmd_init2, timeout=1000)
                time.sleep(0.05)
                # Read response for Init 2
                resp2 = self.device.read(self.ep_in.bEndpointAddress, 64, timeout=1000)
                console.print(f"[dim]FNB58: Init 2 Response: {list(resp2[:4])}...[/dim]")

                # 3. Send Init 2 again (FNB58 requires this to start data stream)
                console.print("[dim]FNB58: Sending Init 2 (repeat)...[/dim]")
                self.ep_out.write(cmd_init2, timeout=1000)
                time.sleep(0.05)
                resp3 = self.device.read(self.ep_in.bEndpointAddress, 64, timeout=1000)
                console.print(f"[dim]FNB58: Init 2 Repeat Response: {list(resp3[:4])}...[/dim]")

                self.connected = True
                self.last_read_time = time.time()
                console.print("[bold green]FNB58: Connection established![/bold green]")
                return True
            else:
                console.print("[bold red]FNB58: Endpoints not found.[/bold red]")
        except Exception as e:
            console.print(f"[bold red]FNB58 Connection Error: {e}[/bold red]")
            self.connected = False
            return False
        
        return False

    def read_data(self):
        current_time = time.time()
        time_delta = current_time - self.last_read_time
        self.last_read_time = current_time

        if self.simulate:
            t = current_time - self.sim_start_time
            noise = random.uniform(-0.01, 0.01)
            self.data["voltage_v"] = 5.0 + (noise * 0.1)
            self.data["current_a"] = 2.0 + noise
            self.data["power_w"] = self.data["voltage_v"] * self.data["current_a"]

            self.sim_capacity_wh += self.data["power_w"] * (time_delta / 3600.0)
            self.sim_capacity_mah += (self.data["current_a"] * 1000.0) * (time_delta / 3600.0)

            self.data["energy_wh"] = self.sim_capacity_wh
            self.data["capacity_mah"] = self.sim_capacity_mah
            self.data["temp_c"] = 30.0 + (t / 60.0)
            self.data["protocol"] = "PD 3.0 (Sim)"
            self.data["dp_v"] = 0.6
            self.data["dm_v"] = 0.6
            self._update_stats(self.data["voltage_v"], self.data["current_a"])
            return self.data

        if not self.connected:
            return None

        try:
            # Send keepalive to maintain data stream
            cmd_data_request = b"\xaa\x83" + b"\x00" * 61 + b"\x9e"
            self.ep_out.write(cmd_data_request, timeout=1000)

            # Drain all buffered packets from the device.
            # The FNB58 streams at ~25 packets/sec (100Hz, 4 samples per packet).
            # We must read all available packets to prevent buffer overflow and
            # to integrate energy/capacity accurately across all samples.
            # First read uses a longer timeout (device needs time to respond to
            # the 0x83 keepalive after the ~1s gap between calls), then short
            # timeouts to drain remaining buffered packets quickly.
            got_data = False
            first_read = True
            while True:
                try:
                    timeout = 1000 if first_read else 50
                    data = self.device.read(self.ep_in.bEndpointAddress, 64, timeout=timeout)
                    first_read = False
                except usb.core.USBTimeoutError:
                    break  # No more buffered packets

                if len(data) != 64 or data[0] != 0xAA:
                    continue

                packet_type = data[1]
                if packet_type != 0x04:
                    continue

                # Process all 4 samples in the packet for accurate integration
                # Each packet contains 4 samples, each 15 bytes, starting at offset 2
                # Sample structure (15 bytes):
                #   0-3: voltage (4 bytes, little endian, /100000 for V)
                #   4-7: current (4 bytes, little endian, /100000 for A)
                #   8-9: D+ voltage (2 bytes, /1000 for V)
                #  10-11: D- voltage (2 bytes, /1000 for V)
                #     12: unknown (constant)
                #  13-14: temperature (2 bytes, /10 for °C)

                for i in range(4):
                    offset = 2 + (15 * i)

                    # Voltage (4 bytes, little endian)
                    v_raw = (data[offset] |
                            (data[offset + 1] << 8) |
                            (data[offset + 2] << 16) |
                            (data[offset + 3] << 24))
                    voltage = v_raw / 100000.0

                    # Current (4 bytes, little endian)
                    c_raw = (data[offset + 4] |
                            (data[offset + 5] << 8) |
                            (data[offset + 6] << 16) |
                            (data[offset + 7] << 24))
                    current = c_raw / 100000.0

                    # D+ voltage (2 bytes)
                    dp_raw = data[offset + 8] | (data[offset + 9] << 8)
                    dp_v = dp_raw / 1000.0

                    # D- voltage (2 bytes)
                    dm_raw = data[offset + 10] | (data[offset + 11] << 8)
                    dm_v = dm_raw / 1000.0

                    # Temperature (2 bytes, offset 13-14, /10 for °C)
                    t_raw = data[offset + 13] | (data[offset + 14] << 8)
                    temp_c = t_raw / 10.0

                    # Apply temperature EMA smoothing
                    if self.temp_ema is None:
                        self.temp_ema = temp_c
                    else:
                        self.temp_ema = temp_c * (1.0 - self.temp_alpha) + self.temp_ema * self.temp_alpha

                    # Integrate energy and capacity for each sample (10ms interval)
                    power = voltage * current
                    dt_hours = self.sample_interval / 3600.0
                    self.data["energy_wh"] += power * dt_hours
                    self.data["capacity_mah"] += (current * 1000.0) * dt_hours

                # Store the last sample values for display
                self.data["voltage_v"] = voltage
                self.data["current_a"] = current
                self.data["power_w"] = voltage * current
                self.data["dp_v"] = dp_v
                self.data["dm_v"] = dm_v
                self.data["temp_c"] = self.temp_ema

                # Update statistics
                self._update_stats(voltage, current)
                got_data = True

            if got_data:
                # Protocol detection (only update after successful reads)
                if self.data["voltage_v"] > 8.0:
                    self.data["protocol"] = "PD / QC (HV)"
                elif self.data["dp_v"] > 2.0:
                    self.data["protocol"] = "QC 2.0/3.0"
                elif self.data["dp_v"] > 0.6 and self.data["dm_v"] > 0.6:
                    self.data["protocol"] = "DCP 1.5A"
                elif self.data["dp_v"] > 0.4:
                    self.data["protocol"] = "Apple 2.4A"
                else:
                    self.data["protocol"] = "Standard 5V"

                return self.data

        except usb.core.USBError as e:
            console.print(f"[red]FNB58 USB Error: {e}[/red]")
        except Exception as e:
            console.print(f"[red]FNB58 Read Error: {e}[/red]")

        return None

    def disconnect(self):
        if self.connected and not self.simulate and self.device:
            try:
                usb.util.release_interface(self.device, self.device.get_active_configuration()[(3,0)])
                usb.util.dispose_resources(self.device)
                print("[green]FNB58 USB interface released.[/green]")
            except Exception as e:
                print(f"[yellow]Warning: Could not release FNB58 USB interface: {e}[/yellow]")
            self.connected = False



# --- SysFS Monitor (Laptop/Hub) ---
class SysPowerSupply:
    def __init__(self, name):
        self.name = name
        self.path = os.path.join(SYS_CLASS_DIR, name)
        self.data = {}

    def read_file(self, filename):
        try:
            with open(os.path.join(self.path, filename), 'r', errors='replace') as f:
                return f.read().strip()
        except (OSError, FileNotFoundError):
            return None

    def refresh(self):
        uevent_data = {}
        uevent_content = self.read_file('uevent')
        if uevent_content:
            for line in uevent_content.splitlines():
                if '=' in line:
                    k, v = line.split('=', 1)
                    uevent_data[k.replace('POWER_SUPPLY_', '').lower()] = v
        
        self.data = {
            'name': self.name,
            'status': self.read_file('status') or uevent_data.get('status', 'Unknown'),
            'type': self.read_file('type') or uevent_data.get('type', 'Unknown'),
            'online': int(self.read_file('online') or 0),
            'voltage_now': self._parse_int(self.read_file('voltage_now')),
            'current_now': self._parse_int(self.read_file('current_now')),
            'capacity': self._parse_int(self.read_file('capacity')),
            'model': self.read_file('model_name'),
            'mfr': self.read_file('manufacturer'),
        }
        
        # Calculate Power
        if self.data['voltage_now'] and self.data['current_now']:
            # Sysfs is usually micro-units
            v = self.data['voltage_now'] / 1_000_000
            a = self.data['current_now'] / 1_000_000
            self.data['power_w'] = round(v * a, 2)
        else:
            self.data['power_w'] = 0.0

    def _parse_int(self, value):
        if value is None: return None
        try: return int(value)
        except ValueError: return None

    def get_formatted_stats(self):
        d = self.data
        v = f"{d['voltage_now']/1_000_000:.2f} V" if d['voltage_now'] else "-"
        a = f"{d['current_now']/1_000_000:.2f} A" if d['current_now'] else "-"
        
        name_display = d['name']
        if d['model']: name_display += f" ({d['model']})"
        elif "BAT" in d['name']: name_display = "Internal Battery"
        
        return {
            "Source": "System",
            "Name": name_display,
            "Voltage": v,
            "Current": a,
            "Power": f"{d['power_w']} W",
            "Energy": f"{d['capacity']}%" if d['capacity'] is not None else "-",
            "Protocol": d['type'],
            "Status": d['status'],
            "Vendor": d['mfr'] or "-",
            "Model": d['model'] or "-"
        }

def scan_sys_supplies():
    supplies = []
    if not os.path.exists(SYS_CLASS_DIR): return supplies
    for name in os.listdir(SYS_CLASS_DIR):
        ps = SysPowerSupply(name)
        ps.refresh()
        # Filter: Show online devices or batteries
        if ps.data['online'] or "BAT" in ps.name:
            supplies.append(ps)
    return supplies

# --- Anker 575 Dock Monitoring ---

class AnkerDockEthernet:
    """Auto-detects and monitors the RTL8153 Gigabit Ethernet on the Anker 575 dock."""

    def __init__(self):
        self.iface = None
        self.iface_path = None
        self.data = {}
        self._prev_counters = {}
        self._prev_time = None

    def detect(self):
        """Walk /sys/class/net/*/device looking for RTL8153 VID:PID."""
        self.iface = None
        self.iface_path = None
        if not os.path.isdir(SYS_CLASS_NET):
            return False
        for name in os.listdir(SYS_CLASS_NET):
            dev_path = os.path.join(SYS_CLASS_NET, name, "device")
            if not os.path.isdir(dev_path):
                continue
            # Walk up to find USB device with idVendor/idProduct
            # The device symlink points to the USB interface; the parent has the IDs
            real = os.path.realpath(dev_path)
            # Check this level and parent levels for idVendor/idProduct
            check = real
            for _ in range(4):
                vid = read_sysfs_file(check, "idVendor")
                pid = read_sysfs_file(check, "idProduct")
                if vid and pid and vid.lower() == RTL8153_VID and pid.lower() == RTL8153_PID:
                    self.iface = name
                    self.iface_path = os.path.join(SYS_CLASS_NET, name)
                    self._prev_counters = {}
                    self._prev_time = None
                    return True
                check = os.path.dirname(check)
                if check in ("", "/"):
                    break
        return False

    def refresh(self):
        """Read current link info and stats. Falls back to detect() on failure."""
        if not self.iface_path:
            self.detect()
            if not self.iface_path:
                self.data = {}
                return

        try:
            self.data = {
                "interface": self.iface,
                "operstate": read_sysfs_file(self.iface_path, "operstate") or "unknown",
                "speed": read_sysfs_int(self.iface_path, "speed"),
                "duplex": read_sysfs_file(self.iface_path, "duplex") or "unknown",
                "carrier": read_sysfs_int(self.iface_path, "carrier"),
                "address": read_sysfs_file(self.iface_path, "address") or "unknown",
                "mtu": read_sysfs_int(self.iface_path, "mtu"),
                "carrier_changes": read_sysfs_int(self.iface_path, "carrier_changes"),
            }

            # Read counters from statistics/
            stats_path = os.path.join(self.iface_path, "statistics")
            counters = {}
            for key in ("rx_bytes", "tx_bytes", "rx_packets", "tx_packets",
                         "rx_errors", "tx_errors", "rx_dropped", "tx_dropped"):
                counters[key] = read_sysfs_int(stats_path, key) or 0
            self.data.update(counters)

            # Compute rates
            now = time.time()
            if self._prev_time and self._prev_counters:
                dt = now - self._prev_time
                if dt > 0:
                    self.data["rx_bytes_sec"] = max(0, (counters["rx_bytes"] - self._prev_counters.get("rx_bytes", 0)) / dt)
                    self.data["tx_bytes_sec"] = max(0, (counters["tx_bytes"] - self._prev_counters.get("tx_bytes", 0)) / dt)
                    self.data["rx_pps"] = max(0, (counters["rx_packets"] - self._prev_counters.get("rx_packets", 0)) / dt)
                    self.data["tx_pps"] = max(0, (counters["tx_packets"] - self._prev_counters.get("tx_packets", 0)) / dt)
                else:
                    self.data["rx_bytes_sec"] = 0.0
                    self.data["tx_bytes_sec"] = 0.0
                    self.data["rx_pps"] = 0.0
                    self.data["tx_pps"] = 0.0
            else:
                self.data["rx_bytes_sec"] = 0.0
                self.data["tx_bytes_sec"] = 0.0
                self.data["rx_pps"] = 0.0
                self.data["tx_pps"] = 0.0
            self._prev_counters = counters
            self._prev_time = now

        except (OSError, FileNotFoundError):
            # Interface disappeared — re-detect next cycle
            self.iface = None
            self.iface_path = None
            self.data = {}


class AnkerDockPower:
    """Detects Anker 575 dock via USB HID and reads BAT0/AC for inferred power delivery."""

    def __init__(self):
        self.detected = False
        self.data = {}

    def detect(self):
        """Scan /sys/bus/usb/devices/ for Anker HID VID:PID."""
        self.detected = False
        if not os.path.isdir(SYS_BUS_USB):
            return False
        for entry in os.listdir(SYS_BUS_USB):
            dev = os.path.join(SYS_BUS_USB, entry)
            vid = read_sysfs_file(dev, "idVendor")
            pid = read_sysfs_file(dev, "idProduct")
            if vid and pid and vid.lower() == ANKER_HID_VID and pid.lower() in ANKER_HID_PIDS:
                self.detected = True
                return True
        return False

    def refresh(self):
        """Read BAT0 and AC sysfs data. Re-detect dock if needed."""
        if not self.detected:
            self.detect()

        # Always read battery/AC (available regardless of dock)
        bat_status = read_sysfs_file(SYS_PS_BAT0, "status") or "Unknown"
        power_now = read_sysfs_int(SYS_PS_BAT0, "power_now")  # microwatts
        voltage_now = read_sysfs_int(SYS_PS_BAT0, "voltage_now")  # microvolts
        capacity = read_sysfs_int(SYS_PS_BAT0, "capacity")
        energy_now = read_sysfs_int(SYS_PS_BAT0, "energy_now")  # microwatt-hours
        energy_full = read_sysfs_int(SYS_PS_BAT0, "energy_full")
        energy_full_design = read_sysfs_int(SYS_PS_BAT0, "energy_full_design")
        cycle_count = read_sysfs_int(SYS_PS_BAT0, "cycle_count")
        ac_online = read_sysfs_int(SYS_PS_AC, "online")

        # Compute derived values
        power_w = power_now / 1_000_000 if power_now else 0.0
        voltage_v = voltage_now / 1_000_000 if voltage_now else 0.0
        energy_now_wh = energy_now / 1_000_000 if energy_now else 0.0
        energy_full_wh = energy_full / 1_000_000 if energy_full else 0.0
        energy_design_wh = energy_full_design / 1_000_000 if energy_full_design else 0.0
        health_pct = (energy_full / energy_full_design * 100) if energy_full and energy_full_design else None

        # Infer delivery state
        if self.detected and ac_online:
            if bat_status == "Charging":
                delivery_state = f"Charging via Dock ({power_w:.1f}W)"
            elif bat_status in ("Full", "Not charging"):
                delivery_state = "Maintaining (Dock Connected)"
            else:
                delivery_state = f"Dock Connected ({bat_status})"
        elif self.detected:
            delivery_state = "Dock Connected (No AC)"
        else:
            delivery_state = "Dock Not Detected"

        self.data = {
            "dock_detected": self.detected,
            "delivery_state": delivery_state,
            "ac_online": bool(ac_online),
            "bat_status": bat_status,
            "power_w": round(power_w, 2),
            "voltage_v": round(voltage_v, 3),
            "capacity_pct": capacity,
            "energy_now_wh": round(energy_now_wh, 2),
            "energy_full_wh": round(energy_full_wh, 2),
            "energy_design_wh": round(energy_design_wh, 2),
            "health_pct": round(health_pct, 1) if health_pct else None,
            "cycle_count": cycle_count,
        }


# --- Main UI & Loop ---

def _build_anker_panel(dock_power, dock_eth):
    """Build the Anker 575 dock middle panel."""
    pw = dock_power.data
    eth = dock_eth.data

    if not pw.get("dock_detected") and not eth:
        return Panel(
            "[dim]Anker 575 dock not detected.\n"
            "Connect dock via USB-C for power delivery and ethernet monitoring.[/dim]",
            title="[bold red]ANKER 575 USB-C DOCK — Not Detected[/bold red]",
            border_style="red",
            padding=(1, 2),
        )

    # --- Power side (left) ---
    pwr_grid = Table.grid(expand=True)
    pwr_grid.add_column(justify="center", ratio=1)
    pwr_grid.add_column(justify="center", ratio=1)

    # Delivery state color
    state = pw.get("delivery_state", "Unknown")
    if "Charging" in state:
        state_style = "bold green"
    elif "Maintaining" in state:
        state_style = "bold cyan"
    elif "No AC" in state:
        state_style = "bold yellow"
    else:
        state_style = "bold red"

    ac_str = "[green]Online[/green]" if pw.get("ac_online") else "[red]Offline[/red]"
    cap_str = f"{pw['capacity_pct']}%" if pw.get("capacity_pct") is not None else "-"
    pwr_w = f"{pw.get('power_w', 0):.1f} W"
    volt_str = f"{pw.get('voltage_v', 0):.3f} V"
    energy_str = f"{pw.get('energy_now_wh', 0):.1f} / {pw.get('energy_full_wh', 0):.1f} Wh"
    health_str = f"{pw['health_pct']:.1f}%" if pw.get("health_pct") is not None else "-"
    cycles_str = str(pw.get("cycle_count")) if pw.get("cycle_count") is not None else "-"

    pwr_grid.add_row(
        Panel(f"[{state_style}]{state}[/{state_style}]", title="Status", border_style="white"),
        Panel(ac_str, title="AC Mains", border_style="white"),
    )
    pwr_grid.add_row(
        Panel(f"[bold]{cap_str}[/bold]", title="Battery %", border_style="yellow"),
        Panel(f"[bold]{pwr_w}[/bold]", title="Bat Power", border_style="gold1"),
    )
    pwr_grid.add_row(
        Panel(volt_str, title="Bat Voltage", border_style="green"),
        Panel(energy_str, title="Energy Wh", border_style="white"),
    )
    pwr_grid.add_row(
        Panel(health_str, title="Health %", border_style="cyan"),
        Panel(cycles_str, title="Cycles", border_style="dim"),
    )

    power_panel = Panel(pwr_grid, title="[bold]Power Delivery[/bold]", border_style="magenta")

    # --- Ethernet side (right) ---
    if eth:
        eth_grid = Table.grid(expand=True)
        eth_grid.add_column(justify="center", ratio=1)
        eth_grid.add_column(justify="center", ratio=1)
        eth_grid.add_column(justify="center", ratio=1)

        # Row 1: Link status, Speed/Duplex, Interface info
        op = eth.get("operstate", "unknown")
        if op == "up":
            link_str = "[bold green]UP[/bold green]"
        elif op == "down":
            link_str = "[bold red]DOWN[/bold red]"
        else:
            link_str = f"[yellow]{op}[/yellow]"

        speed = eth.get("speed")
        duplex = eth.get("duplex", "?")
        speed_str = f"{speed} Mbps" if speed and speed > 0 else "-"
        duplex_str = duplex.capitalize() if duplex and duplex != "unknown" else "-"

        mac = eth.get("address", "?")
        mtu = eth.get("mtu", "?")
        iface = eth.get("interface", "?")

        eth_grid.add_row(
            Panel(link_str, title="Link", border_style="green" if op == "up" else "red"),
            Panel(f"{speed_str}\n{duplex_str}", title="Speed / Duplex", border_style="cyan"),
            Panel(f"{iface}\n[dim]{mac}[/dim]\nMTU {mtu}", title="Interface", border_style="dim"),
        )

        # Row 2: RX Rate, TX Rate, Totals
        rx_rate = format_rate(eth.get("rx_bytes_sec", 0))
        tx_rate = format_rate(eth.get("tx_bytes_sec", 0))
        rx_pps = eth.get("rx_pps", 0)
        tx_pps = eth.get("tx_pps", 0)
        rx_total = format_bytes(eth.get("rx_bytes", 0))
        tx_total = format_bytes(eth.get("tx_bytes", 0))

        eth_grid.add_row(
            Panel(f"[bold green]{rx_rate}[/bold green]\n[dim]{rx_pps:.0f} pps[/dim]", title="RX Rate", border_style="green"),
            Panel(f"[bold cyan]{tx_rate}[/bold cyan]\n[dim]{tx_pps:.0f} pps[/dim]", title="TX Rate", border_style="cyan"),
            Panel(f"RX: {rx_total}\nTX: {tx_total}", title="Totals", border_style="white"),
        )

        # Row 3: Packet counts, Errors/Drops, Link stability
        rx_pkts = eth.get("rx_packets", 0)
        tx_pkts = eth.get("tx_packets", 0)
        rx_err = eth.get("rx_errors", 0)
        tx_err = eth.get("tx_errors", 0)
        rx_drop = eth.get("rx_dropped", 0)
        tx_drop = eth.get("tx_dropped", 0)
        carrier_ch = eth.get("carrier_changes", 0)

        err_style = "red" if (rx_err + tx_err + rx_drop + tx_drop) > 0 else "green"

        eth_grid.add_row(
            Panel(f"RX: {rx_pkts:,}\nTX: {tx_pkts:,}", title="Packets", border_style="dim"),
            Panel(f"[{err_style}]Err: {rx_err}/{tx_err}\nDrop: {rx_drop}/{tx_drop}[/{err_style}]",
                  title="Errors / Drops", border_style=err_style),
            Panel(f"{carrier_ch}", title="Carrier Changes", border_style="dim"),
        )

        eth_panel = Panel(eth_grid, title="[bold]Gigabit Ethernet (RTL8153)[/bold]", border_style="blue")
    else:
        eth_panel = Panel(
            "[dim]Ethernet adapter not detected[/dim]",
            title="[bold]Gigabit Ethernet[/bold]",
            border_style="dim",
        )

    # Combine power + ethernet side by side
    mid_layout = Layout()
    mid_layout.split_row(
        Layout(power_panel, name="dock_power", ratio=1),
        Layout(eth_panel, name="dock_eth", ratio=2),
    )

    dock_title = "[bold magenta]ANKER 575 USB-C DOCK (A83B61A1)[/bold magenta]"
    return Panel(mid_layout, title=dock_title, border_style="magenta")


def generate_dashboard(fnb_device, sys_supplies, dock_power=None, dock_eth=None):
    # --- FNB58 Main Display ---
    if fnb_device.connected or fnb_device.simulate:
        d = fnb_device.data

        # Big Stats Grid
        grid = Table.grid(expand=True)
        grid.add_column(justify="center", ratio=1)
        grid.add_column(justify="center", ratio=1)
        grid.add_column(justify="center", ratio=1)

        # Row 1: The Big Three (V, A, W)
        grid.add_row(
            Panel(f"[bold green]{d['voltage_v']:.4f} V[/bold green]", title="Voltage [dim]VBUS[/dim]", border_style="green"),
            Panel(f"[bold cyan]{d['current_a']:.4f} A[/bold cyan]", title="Current [dim]IBUS[/dim]", border_style="cyan"),
            Panel(f"[bold gold1]{d['power_w']:.4f} W[/bold gold1]", title="Power [dim]PBUS[/dim]", border_style="gold1")
        )

        # Row 2: Integration (Wh, mAh, Temp)
        grid.add_row(
            Panel(f"[bold white]{d['energy_wh']:.4f} Wh[/bold white]", title="Energy", border_style="white"),
            Panel(f"[bold yellow]{d['capacity_mah']:.1f} mAh[/bold yellow]", title="Capacity (Session)", border_style="yellow"),
            Panel(f"[white]{d['temp_c']:.1f} °C[/white]", title="Temp", border_style="blue")
        )

        # Row 3: Technical (D+, D-, Protocol, Session)
        grid.add_row(
            Panel(f"D+: {d['dp_v']:.2f} V\nD-: {d['dm_v']:.2f} V", title="Data Lines", border_style="dim"),
            Panel(f"[bold magenta]{d['protocol']}[/bold magenta]", title="Protocol", border_style="magenta"),
            Panel(f"[bold]{fnb_device.get_session_duration()}[/bold]\n[dim]{datetime.datetime.now().strftime('%H:%M:%S')}[/dim]", title="Session", border_style="dim")
        )

        # Row 4: Statistics (min/avg/max)
        stats = fnb_device.get_stats_display()
        grid.add_row(
            Panel(f"[dim]min / avg / max[/dim]\n{stats['voltage']} V", title="Voltage Stats", border_style="green"),
            Panel(f"[dim]min / avg / max[/dim]\n{stats['current']} A", title="Current Stats", border_style="cyan"),
            Panel("[dim][r][/dim] Reset  [dim][q][/dim] Quit", title="Controls", border_style="dim")
        )

        fnb_panel = Panel(grid, title="[bold blue]EXTERNAL LOAD (FNB58 Remote Display)[/bold blue]", border_style="blue")
    else:
        fnb_panel = Panel(
            "[yellow]Waiting for device connection...[/yellow]\n\n"
            "1. Connect Source to 'Type-C IN'\n"
            "2. Connect Load to 'Type-C OUT'\n"
            "3. Connect PC Port to Laptop",
            title="[bold red]FNB58 Disconnected[/bold red]",
            border_style="red",
            padding=(2, 2)
        )

    # --- Anker 575 Dock (Middle) ---
    if dock_power and dock_eth:
        anker_panel = _build_anker_panel(dock_power, dock_eth)
    else:
        anker_panel = Panel("[dim]Dock monitoring disabled[/dim]",
                            title="ANKER 575 USB-C DOCK", border_style="dim")

    # --- System Table (Bottom) ---
    sys_table = Table(title="Laptop Internal Power Sensors", box=box.SIMPLE, expand=True)
    sys_table.add_column("Source", style="dim")
    sys_table.add_column("Name", style="cyan")
    sys_table.add_column("Status")
    sys_table.add_column("Voltage", justify="right", style="green")
    sys_table.add_column("Current", justify="right", style="green")
    sys_table.add_column("Power", justify="right", style="gold1")
    sys_table.add_column("Level", justify="right")
    sys_table.add_column("Info", style="dim")

    for ps in sys_supplies:
        s = ps.get_formatted_stats()
        # s['Energy'] usually holds capacity % for batteries
        level = s['Energy']
        sys_table.add_row(
            s['Source'],
            s['Name'],
            s['Status'],
            s['Voltage'],
            s['Current'],
            s['Power'],
            level,
            f"{s['Vendor']} {s['Model']}"
        )

    if not sys_supplies:
        sys_table.add_row("-", "No system sensors found", "-", "-", "-", "-", "-", "-")

    # Combine: 3-section layout
    layout = Layout()
    layout.split_column(
        Layout(fnb_panel, name="top", ratio=3),
        Layout(anker_panel, name="middle", ratio=2),
        Layout(Panel(sys_table, border_style="dim"), name="bottom", ratio=1)
    )

    return layout

def log_snapshot(fnb_device, sys_supplies, dock_power=None, dock_eth=None):
    record = {
        "timestamp": datetime.datetime.now().isoformat(),
        "fnb58": fnb_device.data if fnb_device.connected else None,
        "system": [s.data for s in sys_supplies],
        "anker_dock": {
            "power": dock_power.data if dock_power else None,
            "ethernet": dock_eth.data if dock_eth else None,
        },
    }
    try:
        with open(LOG_FILE, 'a') as f:
            f.write(json.dumps(record) + "\n")
    except Exception: pass

def get_key_nonblocking():
    """Check for keypress without blocking. Returns key char or None."""
    if select.select([sys.stdin], [], [], 0)[0]:
        return sys.stdin.read(1)
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--simulate", action="store_true", help="Simulate FNB58")
    args = parser.parse_args()

    console.clear()

    # Initialize FNB58
    fnb = FNB58Device(simulate=args.simulate)
    fnb.connect()

    # Initialize Anker 575 dock monitors
    dock_eth = AnkerDockEthernet()
    dock_power = AnkerDockPower()
    if dock_eth.detect():
        console.print(f"[bold green]Anker Dock Ethernet: Detected ({dock_eth.iface})[/bold green]")
    else:
        console.print("[yellow]Anker Dock Ethernet: RTL8153 not found[/yellow]")
    if dock_power.detect():
        console.print("[bold green]Anker Dock: HID device detected[/bold green]")
    else:
        console.print("[yellow]Anker Dock: HID device not found[/yellow]")

    # Save terminal settings for raw mode
    old_settings = termios.tcgetattr(sys.stdin)

    try:
        # Set terminal to raw mode for non-blocking key detection
        tty.setcbreak(sys.stdin.fileno())

        # Use Layout instead of just Table
        with Live(generate_dashboard(fnb, [], dock_power, dock_eth), refresh_per_second=REFRESH_RATE, screen=True) as live:
            running = True
            while running:
                try:
                    # Check for keyboard input
                    key = get_key_nonblocking()
                    if key:
                        if key.lower() == 'q':
                            running = False
                            break
                        elif key.lower() == 'r':
                            fnb.reset_session()

                    # Refresh FNB58
                    if fnb.connected or fnb.simulate:
                        fnb.read_data()
                    else:
                        fnb.connect()

                    # Refresh Anker dock
                    dock_eth.refresh()
                    dock_power.refresh()

                    # Refresh System Supplies
                    sys_supplies = scan_sys_supplies()

                    live.update(generate_dashboard(fnb, sys_supplies, dock_power, dock_eth))
                    log_snapshot(fnb, sys_supplies, dock_power, dock_eth)

                    time.sleep(REFRESH_RATE)
                except KeyboardInterrupt:
                    break
    finally:
        # Restore terminal settings
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
        fnb.disconnect()

    console.print("\n[bold red]Monitor stopped.[/bold red]")

if __name__ == "__main__":
    main()