#!/usr/bin/env bash
#SBATCH --job-name=benchmark-prepare
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=64G
#SBATCH --time=1-00:00:00
#SBATCH --output=benchmark-prepare-%j.log

DEFAULT_OUTPUT=artifacts

# Submit from the repository root with your usual Python/Lean environment:
# sbatch slurmrunner/prepare-global.sbatch
set -euo pipefail

output="$DEFAULT_OUTPUT"
if (( $# != 0 )); then
  if [[ $# != 2 || $1 != --output || -z $2 ]]; then
    echo 'Usage: sbatch slurmrunner/prepare-global.sbatch [--output DIR]' >&2
    exit 2
  fi
  output="$2"
fi
: "${SLURM_JOB_ID:?Submit this job with sbatch from the repository root}"
cd "${SLURM_SUBMIT_DIR:?Slurm must provide the submission directory}"
test -f lean-toolchain

# Slurm inherits the submitting shell's Python and build-tool environment.
export PYTHONUNBUFFERED=1
export LEAN_NUM_THREADS="${SLURM_CPUS_PER_TASK:-${SLURM_CPUS_ON_NODE:-$(nproc)}}"
export CMAKE_BUILD_PARALLEL_LEVEL="$LEAN_NUM_THREADS"
export MAKEFLAGS="-j$LEAN_NUM_THREADS"
unset LEAN_PATH LEAN_SRC_PATH LEAN_SYSROOT
printf 'Building with %s CPU workers\n' "$LEAN_NUM_THREADS"

mkdir -p "$output"
output="$(cd "$output" && pwd)"
if [[ -e "$output/provers" || -e "$output/suite" ]]; then
  echo "Provers or suite already exist in $output; choose a fresh output directory." >&2
  exit 2
fi
trap 'status=$?; printf "Preparation failed (exit %s). Files: %s\n" "$status" "$output" >&2; exit "$status"' ERR

lake update
lake exe cache get
lake build
python3 slurmrunner/prepare-provers.py --output "$output/provers" --build
python3 slurmrunner/make-suite.py --backends "$output/provers/backends.json" \
  --comparison --compact --output "$output/suite"

printf 'Full benchmark suite ready: %s\n' "$output/suite/vampire-hammer.xml"
