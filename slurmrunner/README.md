# Slurm execution

Shared preparation, suite generation and analysis tools live in [`scripts/`](../scripts/README.md).
Use `scripts/make-suite.py --format slurm` for compact suites, or submit a
standard BenchExec export to the same runner.

```sh
python3 slurmrunner/slurmbench.py submit-packed artifacts/suite/vampire-hammer.xml \
  -p PARTITION --nodes 1 --cpus-per-node 4 --mem 32G --shards 1 \
  --batch-by-module 16 --repetitions 3 -o artifacts/results
python3 slurmrunner/slurmbench.py collect artifacts/results/manifest.json
```

Repetitions run sequentially in separate job arrays. Use `collect --partial`
for progress and `resubmit-shard MANIFEST SHARD` to resume missing work.
`prepare-global.sbatch` builds the configured prover environment and prepares
its compact suite on a compute node. See the [root README](../README.md) for setup.
