#!/usr/bin/env bash
# Godot Build Manager installer: adds the app to your launcher and PATH.
# Runs from the cloned repository; nothing is copied, so `git pull` updates it.
#
#   ./install.sh             install (per user, no root needed)
#   ./install.sh --uninstall remove what this script installed

set -euo pipefail

APP_ID="io.github.t1nkieplayz.GodotBuildManager"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
BIN="$HOME/.local/bin"
DESKTOP="$DATA/applications/$APP_ID.desktop"
ICON="$DATA/icons/hicolor/scalable/apps/$APP_ID.svg"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✔\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m▲\033[0m %s\n' "$*"; }
die()  { printf '  \033[31m✖\033[0m %s\n' "$*" >&2; exit 1; }

refresh_caches() {
    command -v update-desktop-database >/dev/null && update-desktop-database -q "$DATA/applications" || true
    if command -v gtk-update-icon-cache >/dev/null && [ -f "$DATA/icons/hicolor/index.theme" ]; then
        gtk-update-icon-cache -qtf "$DATA/icons/hicolor" || true
    fi
}

if [ "${1:-}" = "--uninstall" ]; then
    bold "Removing Godot Build Manager"
    rm -f "$DESKTOP" "$ICON" "$BIN/godot-build-manager" "$BIN/godot-build"
    refresh_caches
    ok "launcher entry, icon and commands removed"
    warn "Godot's own launcher entry (if installed) is removed with: godot-build integrate --remove"
    warn "Your settings and workspace are kept: ~/.config/godot-build-manager, ~/.local/share/godot-build-manager"
    exit 0
fi

bold "Checking requirements"
PYTHON="$(command -v python3 || true)"
[ -n "$PYTHON" ] || die "python3 not found"
"$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 10))' || die "Python 3.10 or newer is required"
ok "Python $("$PYTHON" -c 'import platform; print(platform.python_version())')"

if ! "$PYTHON" - <<'EOF'
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw
raise SystemExit((Adw.MAJOR_VERSION, Adw.MINOR_VERSION) < (1, 7))
EOF
then
    die "GTK 4 + libadwaita 1.7+ (PyGObject) are required — see the README for your distribution's packages"
fi
ok "GTK 4 + libadwaita $("$PYTHON" -c 'import gi; gi.require_version("Adw","1"); from gi.repository import Adw; print(f"{Adw.MAJOR_VERSION}.{Adw.MINOR_VERSION}")')"

for tool in git scons; do
    if command -v "$tool" >/dev/null; then ok "$tool"; else warn "$tool not found (needed to build Godot)"; fi
done
if command -v clang >/dev/null || command -v gcc >/dev/null; then ok "C++ compiler"; else warn "no clang or gcc found"; fi
command -v gpg >/dev/null && ok "gpg (key backups)" || warn "gpg not found (needed only for encryption key backups)"

bold "Installing"
chmod +x "$HERE/godot_build.py" "$HERE/godot_build_gui.py"
mkdir -p "$BIN" "$(dirname "$DESKTOP")" "$(dirname "$ICON")"
ln -sfn "$HERE/godot_build_gui.py" "$BIN/godot-build-manager"
ln -sfn "$HERE/godot_build.py" "$BIN/godot-build"
ok "commands: godot-build-manager (GUI), godot-build (CLI) in $BIN"
case ":$PATH:" in *":$BIN:"*) ;; *) warn "$BIN is not on your PATH" ;; esac

cp "$HERE/data/$APP_ID.svg" "$ICON"
cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=Godot Build Manager
Comment=Build, encrypt and install custom Godot editors and export templates
Icon=$APP_ID
Exec=$PYTHON "$HERE/godot_build_gui.py"
Terminal=false
Categories=Development;
Keywords=godot;build;export templates;engine;
StartupNotify=true
X-GodotBuild-Managed=true
EOF
refresh_caches
ok "launcher entry: $DESKTOP"

bold "Done"
echo "  Open \"Godot Build Manager\" from your launcher (or run godot-build-manager) and pick your Godot source tree."
