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
its own Lake project, manifest and adapter build directory. All projects share
the root Mathlib checkout, its dependencies and their compiled caches; lean-auto
and solver dependencies remain separate. All prover packages are fetched from
their public repositories at pinned revisions.

```sh
lake update
lake exe cache get
lake build
python3 slurmrunner/prepare-provers.py --output artifacts/provers --build
```

This prepares Vampire-Hammer, Duper, Aesop, `simp_all`, `grind`, upstream
lean-auto+Duper independently. Repeat `--prover NAME` to prepare a
subset. Without `--build`, it only writes projects and `backends.json`; run
`lake update`, `lake exe cache get`, and `lake build` in each project yourself.
Existing project directories are never overwritten. Built-in tactics need no
external prover dependency. Mathlib's own dependencies still appear in manifests.
Generated projects link their baseline package directories to the root checkout
and retain its exact Git pins, so Mathlib is downloaded and compiled once.
Suite generation verifies the shared paths and revisions. Slurm scratch staging
also copies the shared baseline once and links each prover to that local copy.

The harness and every prover adapter build native shared libraries. Each prover
also loads Mathlib's native library for source replay; the first build includes
native compilation of Mathlib and its dependencies. Suite generation rejects
missing or unloaded Mathlib, harness, or adapter libraries. The same recorded
native flags are used locally, in BenchExec, and on Slurm. Regenerate existing
prover projects in a new directory and rebuild before generating new suites.

To prepare the entire suite as a Slurm job, activate your usual Python/Lean
environment and submit from the repository root:

```sh
sbatch slurmrunner/prepare-global.sbatch
```

The job fetches dependencies, builds all six provers and generates the complete
compact comparison suite under `artifacts/suite/` by default. Change
`DEFAULT_OUTPUT` at the top of the job file, or override it with
`sbatch slurmrunner/prepare-global.sbatch --output /shared/benchmark-preparation`.
It uses the existing Python and native build tools, with all allocated CPUs
available to Lake, CMake and Make. Defaults are 16 CPUs, 64 GB and 24 hours;
the compute node needs internet access and the checkout must be on shared storage.
Logs are written to `benchmark-prepare-JOBID.log`. The job prepares the suite;
submit its generated XML using the benchmark-running commands below.

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

## Docker

The image consumes the repositories already compiled by the normal build commands:

```sh
lake update
lake exe cache get
lake build
python3 slurmrunner/prepare-provers.py --output artifacts/provers --build
```

These commands put shared compiled dependencies in the checkout's `.lake/` and
prover-specific builds in `artifacts/provers/`. Supply both, together with the
matching sources and manifests. An `artifacts/provers/` directory alone is not
sufficient. Keep package Git metadata and the shared dependency symlinks.
Build artifacts must be from Linux x86-64 with system libraries compatible with
Ubuntu 24.04; builds produced on macOS or ARM64 cannot be used.

New prover projects use relative paths in their Lake dependencies, registry, and
shared-package symlinks. For already compiled projects with absolute paths, run
`python3 scripts/relativize-provers.py` in their original checkout before staging
them. This updates metadata and symlinks while preserving compiled files; no
native rebuild is needed.

Stage the prepared checkout and its pinned Lean toolchain in a separate artifacts
directory, excluding local credentials and unrelated benchmark output. From the
checkout used to compile:

```sh
mkdir -p artifacts/docker-precompiled/checkout artifacts/docker-precompiled/toolchain
precompiled_dir="$(cd artifacts/docker-precompiled && pwd -P)"
cp -a lean-toolchain lakefile.lean lake-manifest.json LICENCE \
  VampireHammerBenchmark.lean VampireHammerBenchmark scripts slurmrunner Tests \
  .lake "$precompiled_dir/checkout/"
mkdir -p "$precompiled_dir/checkout/artifacts"
cp -a artifacts/provers "$precompiled_dir/checkout/artifacts/"
cp -a "$(lean --print-prefix)/." "$precompiled_dir/toolchain/"

docker buildx build --load --platform linux/amd64 \
  --build-context precompiled="$precompiled_dir" \
  -t vampire-hammer-benchmark .
```

The supplied directory has two entries: `checkout/` contains the matching sources,
root `.lake/`, and `artifacts/provers/`; `toolchain/` contains the pinned Lean
installation (`bin/`, `lib/`, etc.). Use a fresh staging directory when packaging
a different build. You can reuse this directory for later image builds.

The image relocates the checkout to `/opt/benchmark` and the toolchain to
`/opt/lean`; no original host directory is required. Preserve symlinks when
copying and keep dependencies inside the checkout.

Image construction installs system runtime libraries, so it needs internet access
for Ubuntu packages. Lean and all repository builds are copied from the supplied
artifacts; the image does not run `lake update`, `lake exe cache get`, or
`lake build`, or download a Lean toolchain. With networking disabled, it extracts
the theorem catalog using `lake --no-build` and checks that a sample suite can be
prepared for all six backends. Missing or incompatible artifacts cause the image
build to fail. Benchmark-specific source replay and proof attempts still happen
at runtime.

The default backends are Vampire-Hammer, Duper, Aesop, `simp_all`, `grind`, and
lean-auto+Duper.

Run the benchmark offline and persist the suite, logs, and results on the host:

```sh
mkdir -p results
docker run --rm --init --network none \
  --mount type=bind,src="$(pwd)/results",dst=/results \
  vampire-hammer-benchmark
```

Startup automatically runs all six backends on the same 2,000 theorems, with
16 parallel jobs and batches of up to 16 theorems from one module. Selection uses
the existing module-balanced, seeded sampler with seed `mathlib-v4.34.1-docker`;
the same image and seed reproduce the sample. The comparison uses the existing
10-second proof-success and search budgets. Size container resources for 16
concurrent Lean processes, or lower `--jobs` on smaller machines.

Arguments after the image name are forwarded to the local runner. For example:

```sh
docker run --rm --init --network none \
  --mount type=bind,src="$(pwd)/results",dst=/results \
  vampire-hammer-benchmark \
  --count 100 --jobs 4 --seed experiment-2 \
  --prover vampire-hammer --prover duper --output /results/experiment-2

docker run --rm --network none vampire-hammer-benchmark --help
```

The default output is `/results/run`, containing `suite/` and `results/`.
A fresh run refuses to overwrite an existing output directory; use a new
`--output` path or pass `--resume` to continue that directory's saved suite.
Resume uses the saved sample and prover configuration. Keep the image and both
output directories together: suite paths refer to their original container
locations. No downloads or dependency builds are required at runtime.

## Results and reproducibility

For plots, install `python3 -m pip install -r requirements.txt` and use
`python3 slurmrunner/plot-comparison.py --help`. Keep generated suites and raw
results together for reproducibility.

Run tests with `python3 scripts/run-tests.py --suite python` and
`python3 scripts/run-tests.py --suite lean`.

## Generative AI use statement

GPT-6.1 Sol, Claude 5.5, and GLM 5.3 were used in preparing this repository.
