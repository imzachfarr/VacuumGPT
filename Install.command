#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
if [ "$(uname -s)" != Darwin ]; then echo 'This release requires macOS.'; exit 1; fi
ROO_UV="$(command -v uv || true)"
if [ -z "$ROO_UV" ]; then
  if [ -x "$HOME/.local/bin/uv" ]; then ROO_UV="$HOME/.local/bin/uv"; else
    echo 'Installing uv and Python for VacuumGPT (no administrator password needed).'
    curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh
    ROO_UV="$HOME/.local/bin/uv"
  fi
fi
ROO_APP="$HOME/.local/share/vacuumgpt"
mkdir -p "$ROO_APP"
"$ROO_UV" venv --python 3.12 "$ROO_APP/setup-venv"
"$ROO_UV" pip install --python "$ROO_APP/setup-venv/bin/python" -r requirements.lock
"$ROO_APP/setup-venv/bin/python" installer.py --uv "$ROO_UV"
echo 'Press Enter to close.'
read -r
