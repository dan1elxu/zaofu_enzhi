#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
defaults_file="${HOME}/.config/cpa/defaults.env"

if [[ -r "$defaults_file" ]]; then
  # The shared CPA file also loads codex.env and sets NO_PROXY for the relay.
  # shellcheck disable=SC1090
  source "$defaults_file"
else
  echo "ERROR: missing $defaults_file" >&2
  echo "Run the CPA setup first, or export CPA_API_KEY manually." >&2
  exit 2
fi

exec python3 "$script_dir/repro-kanban-app-server.py" "$@"
