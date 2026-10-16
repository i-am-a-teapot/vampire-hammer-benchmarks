# Vampire-Hammer benchmarks

Benchmarks for [Vampire-Hammer](https://github.com/vprover/vampire-hammer),
comparing it with Duper, Aesop, `simp_all`, `grind`, and upstream Lean-auto+Duper.
The lean-auto-style benchmark gives each tactic premises extracted from the original
proof and replays the environment before the target declaration. Successful
proofs must pass kernel checking and target-reuse checks within 10 seconds.

## Build

Use Lean 4.34.1 through elan, Python 3.11+, and the library's C/C++, CMake,
and Flex/Bison toolchain. The current Lake dependency expects a sibling
Vampire-Hammer checkout at `../vampire-hammer-clean`.

```sh
lake update
lake exe cache get
lake build
python3 slurmrunner/prepare-lean-auto.py --output ../upstream-auto-benchmark
(cd ../upstream-auto-benchmark && lake update && lake exe cache get && lake build)
```

## Run the lean-auto-style benchmark

Generate a pilot suite, then submit and collect it with Slurm. Replace
`PARTITION` and adjust resources for your cluster; omit `--count 12` for the
full catalog.

```sh
python3 slurmrunner/make-suite.py --comparison --compact --count 12 \
  --lean-auto-project ../upstream-auto-benchmark --output ../benchmark-pilot
python3 slurmrunner/slurmbench.py submit-packed ../benchmark-pilot/vampire-hammer.xml \
  -p PARTITION --nodes 1 --cpus-per-node 4 --mem 32G --shards 1 \
  --batch-by-module 16 --repetitions 2 -o ../benchmark-results
python3 slurmrunner/slurmbench.py collect ../benchmark-results/manifest.json
```

Generate suites where they will run: paths are absolute. Rebuild and regenerate
after source or dependency changes. Suites record settings, revisions, source
snapshots, and the solver fingerprint.

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
