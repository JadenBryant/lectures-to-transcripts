#!/bin/bash

set -u

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

# Finder-launched Terminal sessions may not include common Homebrew paths.
export PATH="/opt/homebrew/bin:/usr/local/bin:${PATH:-}"

VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python"
NEEDS_SETUP=0

pause_with_error() {
    printf "\n%s\n\n" "$1"
    read -r -p "Press Return to close..." _
    exit 1
}

if [[ ! -x "$VENV_PYTHON" ]]; then
    echo "The project virtual environment was not found."
    NEEDS_SETUP=1
elif ! "$VENV_PYTHON" -c "import groq, yt_dlp" >/dev/null 2>&1; then
    echo "The project virtual environment is missing required packages."
    NEEDS_SETUP=1
fi

if [[ "$NEEDS_SETUP" -eq 1 ]]; then
    printf "\n"
    read -r -p "Set up the app now? [y/N] " REPLY
    case "$REPLY" in
        y|Y|yes|YES|Yes) ;;
        *) exit 1 ;;
    esac

    if [[ ! -x "$VENV_PYTHON" ]]; then
        if ! command -v python3 >/dev/null 2>&1; then
            pause_with_error "Could not find Python 3.10 or newer. Install Python and try again."
        fi
        if ! python3 -c "import sys; raise SystemExit(sys.version_info < (3, 10))"; then
            pause_with_error "Python 3.10 or newer is required."
        fi

        printf "\nCreating .venv...\n"
        if ! python3 -m venv .venv; then
            pause_with_error "Could not create .venv."
        fi
    fi

    printf "\nInstalling dependencies...\n"
    if ! "$VENV_PYTHON" -m pip install --upgrade pip; then
        pause_with_error "Could not upgrade pip."
    fi
    if ! "$VENV_PYTHON" -m pip install -r requirements.txt; then
        pause_with_error "Could not install dependencies."
    fi
fi

exec "$VENV_PYTHON" -m lecture_transcriber
