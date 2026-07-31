#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")/.."

PYTHON_BIN=""
for candidate in python3.12 python3.11; do
  if command -v "$candidate" >/dev/null 2>&1; then
    PYTHON_BIN="$candidate"
    break
  fi
done

if [ -z "$PYTHON_BIN" ]; then
  echo "Требуется Python 3.11 или 3.12." >&2
  exit 1
fi

"$PYTHON_BIN" -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .

echo "Установка завершена."
echo "Выполните: codex login"
echo "Затем: .venv/bin/python -m app doctor"
echo "Запуск: scripts/run.sh"
