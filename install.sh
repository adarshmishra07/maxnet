#!/bin/sh
# maxnet installer. Needs only curl: installs uv if missing (uv brings its own Python), then maxnet.
set -eu

# Tarball, not git+https, so machines without git work too. Switch to "maxnet" once it is on PyPI.
SOURCE="${MAXNET_SOURCE:-https://github.com/adarshmishra07/maxnet/archive/refs/heads/main.tar.gz}"

if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv (a small tool installer that also provides Python)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  export PATH
fi

uv tool install --force "$SOURCE"
echo
maxnet --version
echo "Done. Run: maxnet"
echo "If the command is not found, open a new terminal window."
