#!/bin/sh
set -eu

benchmark_root=${BENCHMARK_ROOT:-/opt/benchmark}

# Prepared registries may also contain other backends. Default to these six,
# while allowing a caller's repeated --prover options to select a subset.
select_defaults=true
select_cases=true
select_experiment=true
for argument in "$@"; do
  case "$argument" in
    --prover|--prover=*) select_defaults=false ;;
    --definition|--definition=*) select_defaults=false; select_cases=false; select_experiment=false ;;
    --test-suite|--test-suite=*) select_cases=false ;;
  esac
done
if [ "$select_experiment" = true ]; then
  set -- --definition "$benchmark_root/configurations/normal.xml" "$@"
fi
if [ "$select_defaults" = true ]; then
  set -- --prover vampire-hammer --prover duper --prover aesop \
    --prover simp_all --prover grind --prover lean-auto-duper "$@"
fi
if [ "$select_cases" = true ]; then
  set -- --catalog /opt/benchmark-data/catalog.jsonl --count 2000 \
    --seed mathlib-v4.34.1-docker "$@"
fi

# Later scalar options override these defaults.
exec python3 "$benchmark_root/scripts/benchmark-mathlib.py" \
  --backends "$benchmark_root/artifacts/provers/backends.json" \
  --jobs 16 \
  --batch-by-module 16 \
  --output /results/run \
  "$@"
