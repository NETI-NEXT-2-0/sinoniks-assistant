#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
uv_bin="${UV_BIN:-uv}"
if [[ -z "${UV_BIN:-}" ]] && ! command -v uv >/dev/null 2>&1 && [[ -x .local/bin/uv ]]; then
  uv_bin="$PWD/.local/bin/uv"
fi
case "${1:-check}" in
  setup)
    "$uv_bin" sync --project backend --locked --group dev
    npm ci --ignore-scripts --prefix outputs/partner-prototype
    ;;
  check)
    "$uv_bin" lock --project backend --check --offline
    "$uv_bin" run --project backend --no-sync ruff check --config backend/pyproject.toml backend/src backend/tests backend/migrations scripts
    "$uv_bin" run --project backend --no-sync mypy --config-file backend/pyproject.toml backend/src
    "$uv_bin" run --project backend --no-sync pytest -q backend/tests -m 'not integration'
    npm test --prefix outputs/partner-prototype
    npm run build --prefix outputs/partner-prototype
    ;;
  integration)
    docker compose --profile checks run --build --rm tests
    ;;
  *)
    echo 'Usage: ./init.sh [setup|check|integration]' >&2
    exit 2
    ;;
esac
