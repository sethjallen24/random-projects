#!/bin/bash
# Double-click launcher for src/app.py (the combined Partners Toolbox app) that
# keeps a Terminal window open, so you can see any error messages -- the Mac
# counterpart of "Launch (with console).bat". Use this for troubleshooting;
# use "Partners Toolbox.app" (built by "Install (Mac).command") for everyday use.
cd "$(dirname "$0")" || exit 1

if [ ! -x .venv/bin/python ]; then
    echo "Partners Toolbox isn't installed yet: double-click \"Install (Mac).command\" first."
    read -n 1 -s -r -p "Press any key to close this window..."
    exit 1
fi

# python.org's Python doesn't use the Mac's certificate store; point HTTPS at certifi's.
if [ -z "$SSL_CERT_FILE" ]; then
    SSL_CERT_FILE="$(.venv/bin/python -c 'import certifi; print(certifi.where())' 2>/dev/null)"
    [ -n "$SSL_CERT_FILE" ] && export SSL_CERT_FILE
fi

.venv/bin/python src/app.py
status=$?
if [ $status -ne 0 ]; then
    echo
    echo "The app exited with an error (see above)."
    read -n 1 -s -r -p "Press any key to close this window..."
fi
