#!/bin/sh
# Install ltm as the `ltm` command.
#
#   sudo ./install.sh          -> /usr/local/bin/ltm (every user)
#   ./install.sh               -> ~/.local/bin/ltm   (just you)
#   ./install.sh --uninstall   (with sudo for the system-wide copy)
#
# POSIX sh, so it works with dash, ash/busybox, bash, ksh, zsh.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
if [ "$(id -u)" -eq 0 ]; then
    DEST=/usr/local/bin
else
    DEST="$HOME/.local/bin"
fi

if [ "${1:-}" = "--uninstall" ]; then
    rm -f "$DEST/ltm"
    echo "Removed $DEST/ltm"
    exit 0
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "ltm needs Python 3.6 or newer. Install it with your package manager:" >&2
    echo "  Debian/Ubuntu/Mint: sudo apt install python3" >&2
    echo "  Fedora/RHEL/Rocky:  sudo dnf install python3" >&2
    echo "  Arch/Manjaro:       sudo pacman -S python" >&2
    echo "  openSUSE:           sudo zypper install python3 python3-curses" >&2
    echo "  Alpine:             sudo apk add python3" >&2
    echo "  Void:               sudo xbps-install python3" >&2
    echo "  Gentoo:             sudo emerge dev-lang/python" >&2
    exit 1
fi
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 6))'; then
    echo "ltm needs Python 3.6 or newer; this is $(python3 --version 2>&1)." >&2
    exit 1
fi
if ! python3 -c 'import curses' 2>/dev/null; then
    echo "Note: Python's curses module is missing, so only the text commands will work" >&2
    echo "(ltm summary, ltm top, ltm proc ...). On openSUSE: sudo zypper install python3-curses" >&2
fi

mkdir -p "$DEST"
cp "$HERE/ltm.py" "$DEST/ltm.tmp"
chmod 0755 "$DEST/ltm.tmp"
mv "$DEST/ltm.tmp" "$DEST/ltm"
echo "Installed $DEST/ltm ($("$DEST/ltm" --version))"

case ":$PATH:" in
    *":$DEST:"*) ;;
    *) echo "Add $DEST to your PATH, e.g.:  echo 'export PATH=\"$DEST:\$PATH\"' >> ~/.profile" ;;
esac
