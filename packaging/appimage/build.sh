#!/usr/bin/env bash
# Build dist/Linux ISO Boot Installer-x86_64.AppImage.
#
# Needs network access (downloads a Python AppImage base and appimagetool) and
# python3 with venv. The AppImage is self-contained: Python and tkinter are
# bundled, so the target system needs only the runtime tools listed in README.md.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$here/../.." && pwd)"
python_version="${PYTHON_VERSION:-3.12}"
venv="${BUILD_VENV:-$root/build/appimage-venv}"
work="$root/build/appimage"
out="$root/dist"
name="Linux ISO Boot Installer-x86_64"

if [ ! -x "$venv/bin/python-appimage" ]; then
    python3 -m venv "$venv"
    "$venv/bin/pip" install --quiet python-appimage
fi

rm -rf "$work"
mkdir -p "$work" "$out"
cp "$here/linux-iso-boot.desktop" "$here/linux-iso-boot.png" "$here/entrypoint.sh" "$work/"

# --no-packaging leaves the AppDir behind. The packaging step of python-appimage
# fails on some versions, so pack it with appimagetool directly.
cd "$root/build"
"$venv/bin/python-appimage" build app --no-packaging -p "$python_version" appimage \
    -x "$root/omarchy_iso_boot.py"

appimagetool="$("$venv/bin/python-appimage" which appimagetool)"
rm -f "$out/$name.AppImage"
ARCH=x86_64 "$appimagetool" "$root/build/$name" "$out/$name.AppImage"
echo "built: $out/$name.AppImage"
