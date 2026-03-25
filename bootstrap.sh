#!/usr/bin/env bash
set -euo pipefail

if [[ ! -d ".venv" ]]; then
  python3.11 -m venv .venv
fi
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
pip install -e .

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example"
fi

echo "Bootstrap complete."
echo "Next steps:"
echo "  source .venv/bin/activate"
echo "  # edit .env (keys + wallet path)"
echo "  creeper-dripper doctor"
echo "  creeper-dripper scan"
echo "  creeper-dripper run --once"
echo "  creeper-dripper run"
