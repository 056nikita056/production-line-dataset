#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")/.."

if [ ! -x ".venv/bin/python" ]; then
  echo "Среда не установлена. Сначала выполните scripts/setup.sh" >&2
  exit 1
fi

exec .venv/bin/python -m app serve
