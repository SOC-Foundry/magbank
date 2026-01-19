#!/bin/bash
echo "Setting up udev rules for FNIRSI FNB58..."

# Create the udev rule file
sudo bash -c 'cat > /etc/udev/rules.d/99-fnirsi.rules <<EOF
SUBSYSTEM=="usb", ATTR{idVendor}=="2e3c", ATTR{idProduct}=="5558", MODE="0666"
EOF'

echo "Reloading udev rules..."
sudo udevadm control --reload-rules
sudo udevadm trigger

echo "Done! You can now run monitor.py without sudo."
