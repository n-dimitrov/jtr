#!/usr/bin/env bash
set -euo pipefail
#
# Install the jtr CLI from local release files — no GitHub, no network
# calls to a repo. Installs uv if it's missing, then installs jtr as a
# uv tool from files you already downloaded.
#
# Auto-detected next to this script:
#   * a wheel (jtr-*.whl), or
#   * a source tree (pyproject.toml) — the case when you extract the
#     release source zip and run the bundled install.sh.
#
# Usage:
#   ./install.sh                 # auto-detect from this folder
#   ./install.sh <wheel|dir>     # install an explicit wheel or source dir

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- resolve the install target --------------------------------------
TARGET="${1:-}"
if [ -z "$TARGET" ]; then
  wheel=$(ls "$HERE"/jtr-*.whl 2>/dev/null | head -n1 || true)
  if [ -n "$wheel" ]; then
    TARGET="$wheel"
  elif [ -f "$HERE/pyproject.toml" ]; then
    TARGET="$HERE"
  fi
fi

if [ -z "$TARGET" ] || [ ! -e "$TARGET" ]; then
  cat >&2 <<'EOF'
Couldn't find jtr to install.

Put this script next to either:
  * the extracted release source folder (contains pyproject.toml), or
  * a jtr-*.whl file

...then re-run it. Or point it explicitly:
  ./install.sh /path/to/jtr-x.y.z-py3-none-any.whl
EOF
  exit 1
fi

# --- ensure uv -------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
  command -v uv >/dev/null 2>&1 || {
    echo "uv installed but not on PATH. Open a new terminal and re-run, or add \$HOME/.local/bin to PATH." >&2
    exit 1
  }
fi

# --- install ---------------------------------------------------------
if [ -d "$TARGET" ]; then
  echo "Installing jtr from source: $TARGET"
else
  echo "Installing jtr from wheel: $TARGET"
fi
uv tool install --force "$TARGET"

echo
echo "Installed. Run 'jtr' to start."
command -v jtr >/dev/null 2>&1 || \
  echo "(If 'jtr' isn't found, open a new terminal so PATH is refreshed.)"
