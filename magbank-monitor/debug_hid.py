import usb.core
import usb.util
import time
import sys

VID = 0x2e3c
PID = 0x5558
INTF_NUM = 3

print(f"Searching for FNB58 (VID={hex(VID)} PID={hex(PID)})...")
dev = usb.core.find(idVendor=VID, idProduct=PID)

if dev is None:
    print("Device not found!")
    sys.exit(1)

print("Device found.")

# Detach kernel driver for Interface 3
if dev.is_kernel_driver_active(INTF_NUM):
    print(f"Detaching kernel driver from Interface {INTF_NUM}...")
    try:
        dev.detach_kernel_driver(INTF_NUM)
        print("Driver detached.")
    except Exception as e:
        print(f"Could not detach driver: {e}")

# Set Config
try:
    dev.set_configuration()
    print("Configuration set.")
except Exception as e:
    print(f"Config set skipped/failed: {e}")

# Claim Interface
print(f"Claiming Interface {INTF_NUM}...")
usb.util.claim_interface(dev, INTF_NUM)
print("Interface claimed.")

# Get Endpoints
cfg = dev.get_active_configuration()
intf = cfg[(INTF_NUM,0)]

ep_out = usb.util.find_descriptor(intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT)
ep_in = usb.util.find_descriptor(intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN)

print(f"EP OUT: {hex(ep_out.bEndpointAddress)} | EP IN: {hex(ep_in.bEndpointAddress)}")

# Test 1: Standard Write (Raw 64 bytes)
print("\n--- Test 1: Raw 64-byte Write ---")
payload = b"\xaa\x81" + b"\x00" * 61 + b"\x8e"
try:
    ep_out.write(payload, timeout=1000)
    print("Write OK. Reading...")
    resp = dev.read(ep_in.bEndpointAddress, 64, timeout=1000)
    print(f"SUCCESS! Read {len(resp)} bytes: {list(resp[:4])}...")
except Exception as e:
    print(f"Failed: {e}")

# Test 2: Write with Report ID 0 (0x00 + 63/64 bytes?) 
# Sometimes HID requires a report ID prefix.
print("\n--- Test 2: Write with Report ID 0 prefix ---")
# Reset/Clear Halt if needed?
try:
    # Some devices need the 0x00 prefix byte if they use Report IDs.
    # If the descriptor didn't specify report IDs, this might fail, but worth a shot.
    payload_padded = b"\x00" + payload 
    ep_out.write(payload_padded, timeout=1000)
    print("Write OK. Reading...")
    resp = dev.read(ep_in.bEndpointAddress, 64, timeout=1000)
    print(f"SUCCESS! Read {len(resp)} bytes: {list(resp[:4])}...")
except Exception as e:
    print(f"Failed: {e}")

usb.util.release_interface(dev, INTF_NUM)
print("\nDone.")
