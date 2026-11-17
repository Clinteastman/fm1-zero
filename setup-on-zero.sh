#!/bin/sh
# Run once ON the CardputerZero, from this folder, to try fm1-zero without building a .deb:
#   sh setup-on-zero.sh
# It installs what the app needs and lets your user reach the FM-1's boot loader. Then:
#   python3 -m fm1zero status
set -e
sudo apt-get install -y python3-pil python3-numpy python3-evdev python3-usb fonts-dejavu-core
sudo sh packaging/postinst
sudo usermod -aG video,input,plugdev,audio "$(id -un)"
echo "Done. Log out and in again (or reboot) so the new groups take effect."
