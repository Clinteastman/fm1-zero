#!/bin/sh
# Builds fm1-zero_<version>_all.deb with plain dpkg-deb. Run it on the CardputerZero (or any Debian), from the
# repository folder:  sh packaging/build-deb.sh
# Layout follows the apps M5 ships: a .desktop in /usr/share/APPLaunch/applications, a program in
# /usr/share/APPLaunch/bin, the icon in /usr/share/APPLaunch/share/images.
set -e
cd "$(dirname "$0")/.."
. packaging/meta.env
ver="$PKG_VERSION-$PKG_REVISION"
root=$(mktemp -d)
trap 'rm -rf "$root"' EXIT
app=$root/usr/share/APPLaunch/apps/fm1-zero

for f in fm1zero/*.py fm1zero/data/*; do
    install -D -m 0644 "$f" "$app/$f"
done
install -D -m 0644 LICENSE "$app/LICENSE"
install -D -m 0644 README.md "$app/README.md"

install -D -m 0755 /dev/stdin "$root/usr/share/APPLaunch/bin/fm1-zero" <<'LAUNCH'
#!/bin/sh
cd /usr/share/APPLaunch/apps/fm1-zero && exec python3 -m fm1zero "$@"
LAUNCH

install -D -m 0644 /dev/stdin "$root/usr/share/APPLaunch/applications/fm1-zero.desktop" <<DESKTOP
[Desktop Entry]
Name=$APP_NAME
Exec=/usr/share/APPLaunch/bin/fm1-zero
Terminal=false
Icon=share/images/fm1-zero.png
Type=Application
DESKTOP

install -D -m 0644 packaging/icon.png "$root/usr/share/APPLaunch/share/images/fm1-zero.png"

# the FM-1's boot loader (4c4a:8057): the people in plugdev may open it, so no root is needed
install -D -m 0644 /dev/stdin "$root/etc/udev/rules.d/70-fm1-zero.rules" <<'RULE'
SUBSYSTEM=="usb", ATTR{idVendor}=="4c4a", ATTR{idProduct}=="8057", MODE="0660", GROUP="plugdev"
RULE

mkdir -p "$root/DEBIAN"
cat >"$root/DEBIAN/control" <<CONTROL
Package: $PKG_NAME
Version: $ver
Architecture: all
Maintainer: fm1-zero <noreply@example.invalid>
Section: APPLaunch
Priority: optional
Depends: $PKG_DEPENDS
Description: $PKG_DESC
CONTROL
echo /etc/udev/rules.d/70-fm1-zero.rules >"$root/DEBIAN/conffiles"
install -m 0755 /dev/stdin "$root/DEBIAN/postinst" <<'POST'
#!/bin/sh
set -e
udevadm control --reload-rules 2>/dev/null || true
udevadm trigger --subsystem-match=usb 2>/dev/null || true
exit 0
POST

out="$PWD/${PKG_NAME}_${ver}_all.deb"
dpkg-deb --root-owner-group --build "$root" "$out" >/dev/null
echo "$out"
dpkg-deb --info "$out" | sed -n '1,20p'
dpkg-deb --contents "$out"
