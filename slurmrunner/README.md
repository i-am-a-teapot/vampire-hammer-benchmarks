# Mathlib benchmarks

The [lean-auto-style benchmark](lean-auto-style.md) uses proof-derived premises and
replays the environment before each declaration. [Comparison suites](comparison.md)
run several tactics on the same eligible theorems. Successful results require
a kernel-checked proof without `sorryAx` or reuse of the target theorem.

Follow the [build and run instructions](../README.md) for a comparison suite.
For Vampire-Hammer alone, generate a suite with:

```sh
python3 slurmrunner/make-suite.py --backends artifacts/provers/backends.json \
  --prover vampire-hammer --strategy lean-auto-style --compact \
  --count 100 --output ../lean-auto-style-suite
```

Omit `--count` for the full catalog. Generate suites on the filesystem where
they will run: paths are absolute. Rebuild and regenerate after source or
dependency changes. `suite-config.json` indexes one configuration per prover, recording revisions,
settings and solver fingerprints in the individual configurations. Each references its own project, Lean environment,
adapter and dependency manifest; Slurm and BenchExec select it with the same
`--config` argument. Each script provides `--help`.

## Run and collect

For Slurm, replace the partition and resource values for your cluster:

```sh
python3 slurmrunner/slurmbench.py submit-packed ../lean-auto-style-suite/vampire-hammer.xml \
  -p PARTITION --nodes 1 --cpus-per-node 4 --mem 32G --shards 1 \
  --repetitions 3 -o ../lean-auto-style-results
python3 slurmrunner/slurmbench.py collect ../lean-auto-style-results/manifest.json
```

Repetitions run sequentially in separate Slurm arrays. Collect the parent
manifest for per-run reports and repetition summaries. Use `collect --partial`
for progress and `resubmit-shard MANIFEST SHARD` with a repetition's manifest
to resume missing work. Partial results must not be reported as full-corpus rates.
Per-theorem proof time is the timing metric; CPU/RSS values from batched runs
belong to the shared batch.

For BenchExec, generate without `--compact`, make `slurmrunner/hammer_bench`
importable through `PYTHONPATH`, and run the generated XML with
`--tool-directory` pointing to this checkout. Collect with:

```sh
python3 slurmrunner/collect-results.py ../lean-auto-style-results \
  --suite ../lean-auto-style-suite --jsonl all-results.jsonl
```

`proved` maps to BenchExec's correct `true`; search failures map to `unknown`.
Invalid proofs and process failures are errors.
