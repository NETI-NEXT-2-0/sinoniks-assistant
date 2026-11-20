#!/usr/bin/env bash
set -euo pipefail
if [[ $# != 2 ]]; then
  echo 'Usage: scripts/run-parser.sh SOURCE OUTPUT_DIRECTORY' >&2
  exit 2
fi
source_path="$(realpath "$1")"
output_path="$(realpath "$2")"
source_name="$(basename "$source_path")"
container_name="partner-parser-$(id -u)-$$"
cleanup() {
  timeout 15s docker rm -f "$container_name" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM
model_args=()
if [[ -n "${PARTNER_PARSER_MODEL_VOLUME:-}" ]]; then
  model_args=(--mount "type=volume,src=$PARTNER_PARSER_MODEL_VOLUME,dst=/models,readonly" --env PARTNER_PARSER_ARTIFACTS=/models)
elif [[ -n "${PARTNER_PARSER_MODELS:-}" ]]; then
  models_path="$(realpath "$PARTNER_PARSER_MODELS")"
  model_args=(--mount "type=bind,src=$models_path,dst=/models,readonly" --env PARTNER_PARSER_ARTIFACTS=/models)
fi
io_args=(--mount "type=bind,src=$source_path,dst=/input/$source_name,readonly" --mount "type=bind,src=$output_path,dst=/output")
if [[ -n "${PARTNER_PARSER_WORK_VOLUME:-}" ]]; then
  work_root="$(realpath "${PARTNER_PARSER_WORK_ROOT:?Set shared parser work root}")"
  if [[ "$source_path" != "$work_root/"* || "$output_path" != "$work_root/"* ]]; then
    echo 'Parser job paths must stay inside the shared work volume' >&2
    exit 2
  fi
  source_directory="$(dirname "$source_path")"
  source_relative="${source_directory#"$work_root/"}"
  output_relative="${output_path#"$work_root/"}"
  io_args=(--mount "type=volume,src=$PARTNER_PARSER_WORK_VOLUME,dst=/input,volume-subpath=$source_relative,readonly" --mount "type=volume,src=$PARTNER_PARSER_WORK_VOLUME,dst=/output,volume-subpath=$output_relative")
fi
deadline="${PARTNER_PARSER_TIMEOUT_SECONDS:-600}"
available_cpus="$(docker info --format '{{.NCPU}}')"
if [[ ! "$available_cpus" =~ ^[1-9][0-9]*$ ]]; then
  echo 'Docker CPU count must be a positive integer' >&2
  exit 2
fi
cpu_limit=4
if (( available_cpus < cpu_limit )); then
  cpu_limit="$available_cpus"
fi
if [[ ! "$deadline" =~ ^[0-9]+$ ]] || (( deadline < 1 || deadline > 600 )); then
  echo 'Parser timeout must be between 1 and 600 seconds' >&2
  exit 2
fi
timeout --signal=TERM --kill-after=15s "${deadline}s" docker run --rm --name "$container_name" \
  "${model_args[@]}" \
  --label "partner-agent.parser=1" \
  --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
  --cpus "$cpu_limit" --memory 4g --memory-swap 4g --pids-limit 128 \
  --tmpfs /tmp:rw,noexec,nosuid,size=512m --user "$(id -u):$(id -g)" \
  "${io_args[@]}" \
  partner-agent-parser:stage3 "/input/$source_name" /output/canonical-docling.json
