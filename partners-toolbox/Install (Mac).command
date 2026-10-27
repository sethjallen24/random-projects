#!/bin/bash
# One-click installer for Partners Toolbox on a Mac -- the Mac counterpart of
# Install.bat. Double-click it once after extracting the zip. It:
#   1. finds a Python 3.9+ that has a working Tk (python.org or Homebrew),
#   2. creates a private Python environment in .venv inside this folder,
#   3. installs every package the four tools need,
#   4. downloads the Chromium browser the BrightPattern tools can use,
#   5. builds "Partners Toolbox.app" in this folder (drag it to the Dock), and
#   6. starts the app.
#
# Safe to run again later (e.g. after an updated zip, or after moving this
# folder) -- it reuses .venv, reinstalls the same packages and rebuilds the app.
#
# If macOS refuses to open this file ("unidentified developer"), open
# Terminal, type  bash  and a space, drag this file into the Terminal window,
# and press Return. See MAC.md.

cd "$(dirname "$0")" || exit 1
DIR="$(pwd)"
APP="$DIR/Partners Toolbox.app"

pause_and_exit() {
    echo
    read -n 1 -s -r -p "Press any key to close this window..."
    echo
    exit "${1:-1}"
}

# ---- 1. A Python with Tk 8.6+ ------------------------------------------------
# Apple's own /usr/bin/python3 (from the Command Line Tools) ships an old Tk
# 8.5 that draws this app incorrectly, so it is only accepted if it has 8.6+.
PY=""
for candidate in \
    /Library/Frameworks/Python.framework/Versions/Current/bin/python3 \
    /opt/homebrew/bin/python3 \
    /usr/local/bin/python3 \
    "$(command -v python3 2>/dev/null)"; do
    [ -n "$candidate" ] && [ -x "$candidate" ] || continue
    # Without the Command Line Tools, /usr/bin/python3 only pops up an installer.
    if [ "$candidate" = /usr/bin/python3 ] && ! xcode-select -p >/dev/null 2>&1; then
        continue
    fi
    if "$candidate" -c 'import sys, tkinter; sys.exit(0 if sys.version_info >= (3, 9) and tkinter.TkVersion >= 8.6 else 1)' >/dev/null 2>&1; then
        PY="$candidate"
        break
    fi
done

if [ -z "$PY" ]; then
    echo "Partners Toolbox needs Python 3.9 or newer with Tk 8.6 (for its window)."
    echo
    echo "Install the latest Python from python.org (the macOS installer includes Tk),"
    echo "then run this file again. Opening the download page now..."
    echo
    echo "(Using Homebrew instead? Run:  brew install python python-tk )"
    open "https://www.python.org/downloads/macos/"
    pause_and_exit 1
fi
echo "Using $PY ($("$PY" --version 2>&1))"

# ---- 2. Private environment in .venv ----------------------------------------
if [ ! -x .venv/bin/python ] || ! .venv/bin/python -c 'import tkinter' >/dev/null 2>&1; then
    echo
    echo "Creating the app's Python environment (.venv)..."
    rm -rf .venv
    if ! "$PY" -m venv .venv; then
        echo
        echo "Could not create .venv -- see the error above."
        pause_and_exit 1
    fi
fi
VPY="$DIR/.venv/bin/python"

# ---- 3. Packages --------------------------------------------------------------
echo
echo "Installing Python packages (this can take a minute)..."
"$VPY" -m pip install --upgrade pip >/dev/null 2>&1
if ! "$VPY" -m pip install -r requirements.txt; then
    echo
    echo "Package install failed -- see the error above. Common fixes:"
    echo "  - Check your internet connection and run this file again."
    echo "  - Install the latest Python from python.org and run this file again."
    pause_and_exit 1
fi

# ---- 4. Chromium --------------------------------------------------------------
echo
echo "Installing the Chromium browser the BrightPattern tools use..."
if ! "$VPY" -m playwright install chromium; then
    echo
    echo "Chromium install failed -- see the error above."
    pause_and_exit 1
fi

# ---- 5. Partners Toolbox.app -------------------------------------------------
echo
echo "Building Partners Toolbox.app..."
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp assets/app_icon.icns "$APP/Contents/Resources/app_icon.icns"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>Partners Toolbox</string>
    <key>CFBundleDisplayName</key><string>Partners Toolbox</string>
    <key>CFBundleIdentifier</key><string>local.partners-toolbox</string>
    <key>CFBundleExecutable</key><string>Partners Toolbox</string>
    <key>CFBundleIconFile</key><string>app_icon</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>LSMinimumSystemVersion</key><string>10.15</string>
    <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST
# The launcher remembers this folder. If the app is later copied to
# /Applications or the Dock, it still finds the folder; if the FOLDER moves,
# run this installer again (like re-running create_desktop_shortcut.vbs).
cat > "$APP/Contents/MacOS/Partners Toolbox" <<LAUNCHER
#!/bin/bash
DIR="$DIR"
if [ ! -f "\$DIR/src/app.py" ]; then
    DIR="\$(cd "\$(dirname "\$0")/../../.." && pwd)"   # the app is still inside the folder
fi
if [ ! -x "\$DIR/.venv/bin/python" ] || [ ! -f "\$DIR/src/app.py" ]; then
    osascript -e 'display alert "Partners Toolbox" message "Could not find the Partners Toolbox folder. Run \\"Install (Mac).command\\" in that folder again."'
    exit 1
fi
cd "\$DIR" || exit 1
mkdir -p logs
# python.org's Python doesn't use the Mac's certificate store; point HTTPS at certifi's.
if [ -z "\$SSL_CERT_FILE" ]; then
    SSL_CERT_FILE="\$("\$DIR/.venv/bin/python" -c 'import certifi; print(certifi.where())' 2>/dev/null)"
    [ -n "\$SSL_CERT_FILE" ] && export SSL_CERT_FILE
fi
exec "\$DIR/.venv/bin/python" "\$DIR/src/app.py" >>"\$DIR/logs/mac-launch.log" 2>&1
LAUNCHER
chmod +x "$APP/Contents/MacOS/Partners Toolbox" "Launch (Mac, with Terminal).command" 2>/dev/null

# Files from a downloaded zip are quarantined, which would make macOS block the
# app and the launch file. This folder's contents came from you, so clear it.
xattr -dr com.apple.quarantine "$DIR" >/dev/null 2>&1

# Keep macOS's App Nap from slowing automation while the window is in the
# background (it throttles Python apps that aren't in front).
defaults write org.python.python NSAppSleepDisabled -bool YES >/dev/null 2>&1

echo
echo "Install complete. Starting Partners Toolbox..."
echo "  - Drag \"Partners Toolbox.app\" (in this folder) to your Dock to keep it there."
echo "  - Use \"Launch (Mac, with Terminal).command\" any time you want to see log"
echo "    output in a Terminal window."
open "$APP"
