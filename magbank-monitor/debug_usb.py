import usb.core
import usb.util
import sys

VID = 0x2e3c
PID = 0x5558

print(f"Searching for device VID={hex(VID)} PID={hex(PID)}...")
dev = usb.core.find(idVendor=VID, idProduct=PID)

if dev is None:
    print("Device not found! Check connection.")
    sys.exit(1)

print("Device found! Dumping descriptors...")

# Try to set configuration and iterate through it
try:
    # Iterate through all configurations
    for cfg_idx in range(dev.bNumConfigurations):
        cfg = dev.get_active_configuration() # Get the currently active configuration
        print(f"\n--- Configuration {cfg.bConfigurationValue} ---")
        
        # Iterate through all interfaces in this configuration
        for intf in cfg:
            print(f"  --- Interface {intf.bInterfaceNumber}, Alt Setting {intf.bAlternateSetting} ---")
            try:
                # This string reading sometimes fails if permissions are bad, so wrap in try/except
                interface_string = usb.util.get_string(dev, intf.iInterface)
            except (ValueError, usb.core.USBError):
                interface_string = "N/A"
            print(f"    Class: {intf.bInterfaceClass} ({interface_string}) ")
            
            # Iterate through all endpoints in this interface
            for ep in intf:
                print(f"    Endpoint: {hex(ep.bEndpointAddress)}")
                print(f"      Direction: {'IN' if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN else 'OUT'}")
                print(f"      Type: {usb.util.endpoint_type(ep.bmAttributes)}")
                print(f"      Max Packet Size: {ep.wMaxPacketSize}")

except usb.core.USBError as e:
    print(f"Error accessing device descriptors: {e}")
    print("You might need to run with sudo or check udev rules.")

print("\nDescriptor dump complete.")
