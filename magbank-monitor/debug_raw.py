#!/usr/bin/env python3
"""Debug script to dump raw FNB58 packet data"""

import usb.core
import usb.util
import time

VID_FNB58 = 0x2E3C
PID_FNB58 = 0x5558

def main():
    print("Looking for FNB58...")
    dev = usb.core.find(idVendor=VID_FNB58, idProduct=PID_FNB58)

    if dev is None:
        print("Device not found!")
        return

    print("Found FNB-58 device")

    # Detach kernel drivers from ALL interfaces (important!)
    for cfg in dev:
        for intf in cfg:
            intf_num = intf.bInterfaceNumber
            if dev.is_kernel_driver_active(intf_num):
                print(f"Detaching kernel driver from interface {intf_num}...")
                try:
                    dev.detach_kernel_driver(intf_num)
                except usb.core.USBError as e:
                    print(f"  Warning: {e}")

    intf_number = 3

    try:
        dev.set_configuration()
    except usb.core.USBError as e:
        print(f"Set configuration warning (may be ok): {e}")

    usb.util.claim_interface(dev, intf_number)

    cfg = dev.get_active_configuration()
    intf = cfg[(intf_number, 0)]

    ep_out = usb.util.find_descriptor(intf, bEndpointAddress=0x03)
    ep_in = usb.util.find_descriptor(intf, bEndpointAddress=0x83)

    print(f"Endpoints: OUT={hex(ep_out.bEndpointAddress)} IN={hex(ep_in.bEndpointAddress)}")

    # Init sequence
    print("\nSending init sequence...")
    ep_out.write(b"\xaa\x81" + b"\x00" * 61 + b"\x8e")
    time.sleep(0.05)
    resp1 = dev.read(ep_in.bEndpointAddress, 64, timeout=1000)
    print(f"Init 1 response: {list(resp1[:8])}...")

    ep_out.write(b"\xaa\x82" + b"\x00" * 61 + b"\x96")
    time.sleep(0.05)
    resp2 = dev.read(ep_in.bEndpointAddress, 64, timeout=1000)
    print(f"Init 2 response: {list(resp2[:8])}...")

    print("\n--- Reading data packets (Ctrl+C to stop) ---\n")

    try:
        for i in range(20):
            # Request data
            ep_out.write(b"\xaa\x83" + b"\x00" * 61 + b"\x9e")
            data = dev.read(ep_in.bEndpointAddress, 64, timeout=1000)

            print(f"Packet {i+1}: len={len(data)}")
            print(f"  Header: {hex(data[0])} {hex(data[1])}")

            if data[0] == 0xAA and data[1] == 0x04:
                # Parse first sample
                offset = 2
                v_raw = data[offset] | (data[offset+1] << 8) | (data[offset+2] << 16) | (data[offset+3] << 24)
                c_raw = data[offset+4] | (data[offset+5] << 8) | (data[offset+6] << 16) | (data[offset+7] << 24)
                dp_raw = data[offset+8] | (data[offset+9] << 8)
                dm_raw = data[offset+10] | (data[offset+11] << 8)
                t_raw = data[offset+13] | (data[offset+14] << 8)

                print(f"  Sample 1 raw bytes: {list(data[2:17])}")
                print(f"  V_raw={v_raw} -> {v_raw/100000:.5f}V")
                print(f"  C_raw={c_raw} -> {c_raw/100000:.5f}A")
                print(f"  DP_raw={dp_raw} -> {dp_raw/1000:.3f}V")
                print(f"  DM_raw={dm_raw} -> {dm_raw/1000:.3f}V")
                print(f"  T_raw={t_raw} -> {t_raw/10:.1f}°C")

            print()
            time.sleep(0.5)

    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        usb.util.release_interface(dev, intf_number)
        usb.util.dispose_resources(dev)

if __name__ == "__main__":
    main()
