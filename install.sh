#!/bin/sh
set -eu
#
# Install or upgrade the jtr CLI. Installs uv if it's missing, then
# installs jtr as a uv tool.
#
# One-liner (latest release, straight from GitHub):
#   curl -LsSf https://n-dimitrov.github.io/jtr/install.sh | sh
#
# Offline / pinned, from files you already downloaded:
#   ./install.sh                 # a jtr-*.whl or source tree next to this script
#   ./install.sh <wheel|dir>     # an explicit wheel or source dir
#
# Environment:
#   JTR_VERSION=1.1.0            # install that release instead of the latest
#
# Re-running it upgrades. Plain POSIX sh on purpose: `curl | sh` runs
# under dash on Debian/Ubuntu.

REPO="n-dimitrov/jtr"

fail() {
  echo "$*" >&2
  exit 1
}

# --- resolve the install target --------------------------------------
# Local files win when the script sits next to them (the extracted
# release zip). Piped from curl there is no script dir, so $0 is the
# shell and this stays empty.
TARGET="${1:-}"
if [ -n "$TARGET" ] && [ ! -e "$TARGET" ]; then
  fail "Not found: $TARGET"
fi
if [ -z "$TARGET" ]; then
  case "$0" in
    *install.sh)
      HERE="$(cd "$(dirname "$0")" && pwd)"
      wheel=$(ls "$HERE"/jtr-*.whl 2>/dev/null | head -n1 || true)
      if [ -n "$wheel" ]; then
        TARGET="$wheel"
      elif [ -f "$HERE/pyproject.toml" ]; then
        TARGET="$HERE"
      fi
      ;;
  esac
fi

if [ -z "$TARGET" ]; then
  command -v curl >/dev/null 2>&1 || fail "curl is required to download jtr."
  if [ -n "${JTR_VERSION:-}" ]; then
    version="${JTR_VERSION#v}"
  else
    # /releases/latest redirects to /releases/tag/vX.Y.Z — no API call,
    # so no rate limit to hit from behind a shared proxy.
    latest=$(curl -fsSLI -o /dev/null -w '%{url_effective}' \
      "https://github.com/$REPO/releases/latest") ||
      fail "Couldn't reach github.com to find the latest jtr release."
    version="${latest##*/v}"
    case "$version" in
      [0-9]*.[0-9]*) ;;
      *) fail "Couldn't determine the latest jtr release (got: $latest)." ;;
    esac
  fi
  TARGET="https://github.com/$REPO/releases/download/v$version/jtr-$version-py3-none-any.whl"
fi

# --- ensure uv -------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  PATH="$HOME/.local/bin:$PATH"
  export PATH
  command -v uv >/dev/null 2>&1 ||
    fail "uv installed but not on PATH. Open a new terminal and re-run, or add \$HOME/.local/bin to PATH."
fi

# --- install ---------------------------------------------------------
echo "Installing jtr from $TARGET"
uv tool install --force "$TARGET"

# --- refresh the Claude Code skill -----------------------------------
# Copies of the /jtr skill installed by an older jtr would otherwise go
# stale. Only existing, unedited copies are touched; releases before
# 1.2.0 have no `skill` command, hence the probe.
JTR="$(uv tool dir --bin 2>/dev/null || true)/jtr"
[ -x "$JTR" ] || JTR="jtr"
if "$JTR" skill --help >/dev/null 2>&1; then
  "$JTR" skill update || true
fi

echo
echo "Installed $("$JTR" --version 2>/dev/null || echo jtr). Run 'jtr' to start."
command -v jtr >/dev/null 2>&1 ||
  echo "('jtr' isn't on PATH yet: run 'uv tool update-shell', then open a new terminal.)"
