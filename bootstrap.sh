#!/usr/bin/env bash
set -euo pipefail

python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example"
fi

echo "Done. Edit .env, then run:"
echo "  source .venv/bin/activate"
echo "  creeper-dripper scan"
echo "  creeper-dripper run --once"
