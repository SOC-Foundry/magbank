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
            return self.data

        if not self.connected:
            return None

        try:
            # Request data from device
            cmd_data_request = b"\xaa\x83" + b"\x00" * 61 + b"\x9e"
            self.ep_out.write(cmd_data_request, timeout=1000)

            data = self.device.read(self.ep_in.bEndpointAddress, 64, timeout=1000)

            if len(data) == 64 and data[0] == 0xAA:
                packet_type = data[1]
                if packet_type == 0x04:
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

                    # Protocol detection
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

        except usb.core.USBTimeoutError:
            console.print("[yellow]FNB58: Read timeout[/yellow]")
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

# --- Main UI & Loop ---

def generate_dashboard(fnb_device, sys_supplies):
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

    # Combine
    layout = Layout()
    layout.split_column(
        Layout(fnb_panel, name="top", ratio=2),
        Layout(Panel(sys_table, border_style="dim"), name="bottom", ratio=1)
    )
    
    return layout

def log_snapshot(fnb_device, sys_supplies):
    record = {
        "timestamp": datetime.datetime.now().isoformat(),
        "fnb58": fnb_device.data if fnb_device.connected else None,
        "system": [s.data for s in sys_supplies]
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

    # Save terminal settings for raw mode
    old_settings = termios.tcgetattr(sys.stdin)

    try:
        # Set terminal to raw mode for non-blocking key detection
        tty.setcbreak(sys.stdin.fileno())

        # Use Layout instead of just Table
        with Live(generate_dashboard(fnb, []), refresh_per_second=REFRESH_RATE, screen=True) as live:
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

                    # Refresh System Supplies
                    sys_supplies = scan_sys_supplies()

                    live.update(generate_dashboard(fnb, sys_supplies))
                    log_snapshot(fnb, sys_supplies)

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