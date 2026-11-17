#!/usr/bin/env bash
set -euo pipefail

for f in fm1zero/*.py; do
    install -D -m 0644 "$f" "$STAGE$APP_INSTALL_DIR/$f"
done
for f in fm1zero/data/*; do
    install -D -m 0644 "$f" "$STAGE$APP_INSTALL_DIR/$f"
done
install -D -m 0644 LICENSE "$STAGE$APP_INSTALL_DIR/LICENSE"
install -D -m 0644 README.md "$STAGE$APP_INSTALL_DIR/README.md"

tmp=$(mktemp)
cat >"$tmp" <<'LAUNCH'
#!/bin/sh
cd /usr/share/APPLaunch/apps/fm1-zero && exec python3 -m fm1zero "$@"
LAUNCH
install -D -m 0755 "$tmp" "$STAGE$INSTALL_PREFIX/bin/fm1-zero"
rm -f "$tmp"
