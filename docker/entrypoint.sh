#!/bin/sh
set -eu

benchmark_root=${BENCHMARK_ROOT:-/opt/benchmark}

# Prepared registries may also contain other backends. Default to these six,
# while allowing a caller's repeated --prover options to select a subset.
select_defaults=true
for argument in "$@"; do
  case "$argument" in
    --prover|--prover=*) select_defaults=false ;;
  esac
done
if [ "$select_defaults" = true ]; then
  set -- --prover vampire-hammer --prover duper --prover aesop \
    --prover simp_all --prover grind --prover lean-auto-duper "$@"
fi

# Later scalar options override these defaults.
exec python3 "$benchmark_root/scripts/benchmark-mathlib.py" \
  --backends "$benchmark_root/artifacts/provers/backends.json" \
  --catalog /opt/benchmark-data/catalog.jsonl \
  --comparison \
  --count 2000 \
  --seed mathlib-v4.34.1-docker \
  --jobs 16 \
  --batch-by-module 16 \
  --output /results/run \
  "$@"
