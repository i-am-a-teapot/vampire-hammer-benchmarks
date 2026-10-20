# Vampire-Hammer benchmarks

Compare Vampire-Hammer, Duper, Aesop, `simp_all`, `grind`, and Lean-auto+Duper
on Mathlib. Each prover uses an isolated Lake project with shared Mathlib caches.
Testcases use proof-derived premises and replay the environment before the target
declaration. Successful proofs must pass kernel checking and target-reuse checks
within the default 10-second proof budget.

## Build

Requirements: Lean 4.34.1 through elan, Python 3.11+, C/C++, CMake, and Flex/Bison.

```sh
lake update
lake exe cache get
lake build
python3 scripts/prepare-provers.py --definition configurations/provers.xml \
  --output artifacts/provers --build
```

Repeat `--prover NAME` to prepare a subset. Existing prover directories are never
overwritten. Rebuild and regenerate suites after source or dependency changes.
Repository pins, checkout directories, imports and tactic wrappers are defined
in [prover XML files](configurations/README.md); case options belong to experiments.
For cluster preparation, `sbatch slurmrunner/prepare-global.sbatch` builds all
provers and prepares the full suite under `artifacts/suite/`.

## Generate testcases and configure experiments

Testcase selection is independent of prover configuration:

```sh
python3 scripts/make-suite.py --tasks-only --count 100 \
  --output artifacts/mathlib-cases
```

Then use an XML experiment file to select those testcases and define named prover
configurations, including several configurations of the same prover. Unsupported
Lean options are omitted and recorded. See the [configuration example](scripts/README.md).
Omit `--count` for all eligible theorems; use `--seed` for reproducible samples.

## Slurmbench walkthrough

After building, generate a 100-theorem suite for the standard six-prover
experiment. Replace `PARTITION` and adjust resources for your cluster:

```sh
python3 scripts/make-suite.py --definition configurations/normal.xml \
  --backends artifacts/provers/backends.json --format slurm --count 100 \
  --output artifacts/slurm-suite
python3 slurmrunner/slurmbench.py submit-packed artifacts/slurm-suite/vampire-hammer.xml \
  -p PARTITION --nodes 1 --cpus-per-node 4 --mem 32G --shards 1 \
  --repetitions 3 --batch-by-module 16 -o artifacts/slurm-results
```

After the jobs finish, collect the parent manifest to produce result XML,
comparison reports and repetition summaries:

```sh
python3 slurmrunner/slurmbench.py collect artifacts/slurm-results/manifest.json
```

For local execution, use `run-local` instead of submitting:

```sh
python3 slurmrunner/slurmbench.py run-local artifacts/slurm-suite/vampire-hammer.xml \
  --jobs 4 --batch-by-module 16 -o artifacts/local-results
```

## BenchExec walkthrough

After the common build, install BenchExec in your Python environment and follow
its [Linux cgroup setup instructions](https://github.com/sosy-lab/benchexec/blob/main/doc/INSTALL.md).
Export the same experiment as standard XML and YAML tasks:

```sh
python3 -m pip install benchexec
python3 scripts/make-suite.py --definition configurations/normal.xml \
  --backends artifacts/provers/backends.json --format benchexec --count 100 \
  --output artifacts/benchexec-suite
PYTHONPATH="$(pwd)/scripts${PYTHONPATH:+:$PYTHONPATH}" benchexec \
  --no-container --tool-directory "$(pwd)" \
  -o artifacts/benchexec-results/ artifacts/benchexec-suite/vampire-hammer.xml
```

The tool adapter runs each theorem with its prover config. `--no-container`
keeps the recorded host project and toolchain paths accessible. Collect the
BenchExec logs and export per-theorem results with:

```sh
python3 scripts/collect-results.py artifacts/benchexec-results \
  --suite artifacts/benchexec-suite --jsonl artifacts/benchexec-results.jsonl
```

Generate suites on the filesystem where they will run; paths are absolute.
Both walkthroughs use the same seeded sample; omit `--count` for the full catalog,
or replace it with `--test-suite artifacts/mathlib-cases` to reuse a saved selection.
Use fresh output directories and keep suites and raw results together.
See [suite options](scripts/README.md), [Slurm execution](slurmrunner/README.md),
[methodology and analysis](docs/comparison.md), and [Docker usage](docker/README.md).

## Validation

```sh
python3 scripts/run-tests.py --suite python
python3 scripts/run-tests.py --suite lean
```

For plots, install `requirements.txt` and run
`python3 scripts/plot-comparison.py --help`.

## Generative AI use statement

GPT-6.1 Sol, Claude 5.5, and GLM 5.3 were used in preparing this repository.
