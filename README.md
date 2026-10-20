# Vampire-Hammer benchmarks

Benchmarks for [Vampire-Hammer](https://github.com/vprover/vampire-hammer),
comparing it with Duper, Aesop, `simp_all`, `grind`, and upstream Lean-auto+Duper.
The lean-auto-style benchmark gives each tactic premises extracted from the original
proof and replays the environment before the target declaration. Successful
proofs must pass kernel checking and target-reuse checks within 10 seconds.

## Build

Use Lean 4.34.1 through elan, Python 3.11+, and the library's C/C++, CMake,
and Flex/Bison toolchain for external solvers. The root project contains the
shared replay, timing, premise extraction and proof-audit code. Each prover has
its own Lake project, manifest and build directory; no lean-auto override is
shared between Vampire-Hammer and Duper. All prover packages are fetched from
their public repositories at pinned revisions.

```sh
lake update
lake exe cache get
lake build
python3 slurmrunner/prepare-provers.py --output artifacts/provers --build
```

This prepares Vampire-Hammer, Duper, Aesop, `simp_all`, `grind`, upstream
lean-auto+Duper, and Lean-SMT independently. Repeat `--prover NAME` to prepare a
subset. Without `--build`, it only writes projects and `backends.json`; run
`lake update`, `lake exe cache get`, and `lake build` in each project yourself.
Existing project directories are never overwritten. Built-in tactics need no
external prover dependency. Mathlib's own dependencies still appear in manifests.

## Run the lean-auto-style benchmark

Generate a pilot suite, then submit and collect it with Slurm. Replace
`PARTITION` and adjust resources for your cluster; omit `--count 12` for the
full catalog.

```sh
python3 slurmrunner/make-suite.py --backends artifacts/provers/backends.json \
  --comparison --compact --count 12 --output ../benchmark-pilot
python3 slurmrunner/slurmbench.py submit-packed ../benchmark-pilot/vampire-hammer.xml \
  -p PARTITION --nodes 1 --cpus-per-node 4 --mem 32G --shards 1 \
  --batch-by-module 16 --repetitions 2 -o ../benchmark-results
python3 slurmrunner/slurmbench.py collect ../benchmark-results/manifest.json
```

Generate suites where they will run: paths are absolute. Rebuild and regenerate
after source or dependency changes. Suites record settings, revisions, source
snapshots, and the solver fingerprint.

Use `--prover NAME` on `make-suite.py` to select any subset from the registry.
Every prover uses the same on-the-fly environment generation and runner;
per-prover configuration files select the isolated Lean process. No standalone
problem export is required. Both single tasks and `--batch-by-module` use that
configuration, including node-local staging with `--scratch-root`.

For local execution, use the same suite generator and worker without Slurm:

```sh
python3 scripts/benchmark-mathlib.py --backends artifacts/provers/backends.json \
  --comparison --count 12 --jobs 4 --batch-by-module 16 \
  --output artifacts/local-comparison
```

This writes the suite under `suite/` and the same collected results as Slurm
under `results/`. Add `--extract-only` to prepare without running, or `--resume`
to continue a saved suite. You can also run an existing suite with
`python3 slurmrunner/slurmbench.py run-local SUITE/vampire-hammer.xml -o RESULTS`.
The old root-only combined prover build is replaced by `--backends`.

See the [comparison protocol](slurmrunner/comparison.md) for optional baselines
and analysis, [premise methodology](slurmrunner/lean-auto-style.md) for Vampire-Hammer-only
runs, and [execution details](slurmrunner/README.md) for runner options.

## Results and reproducibility

For plots, install `python3 -m pip install -r requirements.txt` and use
`python3 slurmrunner/plot-comparison.py --help`. Keep generated suites and raw
results together for reproducibility.

Run tests with `python3 scripts/run-tests.py --suite python` and
`python3 scripts/run-tests.py --suite lean`.

## Generative AI use statement

GPT-6.1 Sol, Claude 5.5, and GLM 5.3 were used in preparing this repository.
