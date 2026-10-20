# Docker BenchExec sample

The image contains sources, build tools and BenchExec. On first startup it downloads
the pinned Lean toolchain, fetches the locked dependencies, and compiles Mathlib,
the benchmark harness and the selected provers **without downloading build caches**.
Then it generates a sample and runs BenchExec automatically.

The compiled environment lives in a persistent Docker volume, keeping those large
files out of the image. A full Mathlib source build takes substantial time and
still needs several GB of disk space in the volume. Interrupted builds resume;
subsequent runs reuse a completed environment.

Build and run from the repository root; no host Lean installation or precompiled
checkout is needed:

```sh
docker build -t vampire-hammer-benchmark:sample .
docker volume create vampire-hammer-build
mkdir -p artifacts/docker-results

docker run --rm --privileged --cap-drop=ALL --cgroupns=private \
  --mount type=volume,src=vampire-hammer-build,dst=/var/lib/benchmark \
  --mount type=bind,src="$(pwd)/artifacts/docker-results",dst=/results \
  vampire-hammer-benchmark:sample \
  --count 1000 --jobs 1 --output /results/large-1000
```

The first startup needs internet access for the Lean release and Git checkouts.
Lean uses the official prebuilt compiler pinned in `lean-toolchain`; Mathlib,
the harness, tactic libraries and Vampire are compiled inside the container.
Defaults are `normal_small.xml` and `provers_sample.xml` (Vampire-Hammer + `grind`),
100 sampled theorems and one benchmark worker. `--build-jobs 2` controls compilation
parallelism independently of BenchExec's `--jobs`. Use a fresh output path per run.

To prepare the volume separately, without starting BenchExec:

```sh
docker run --rm \
  --mount type=volume,src=vampire-hammer-build,dst=/var/lib/benchmark \
  vampire-hammer-benchmark:sample --prepare-only --build-jobs 2
```

Once preparation completes, add `--network none` to benchmark commands for offline
runs. Always mount the same build volume. Only one container may use it at a time;
use separate volumes for concurrent runs. Changed image sources or prover
definitions create a separate workspace in the volume. Existing builds are kept.
Use a new volume when you want a completely fresh compilation.

To build all six provers and select the standard experiment:

```sh
docker build \
  --build-arg PROVERS=configurations/provers.xml \
  --build-arg EXPERIMENT=configurations/normal.xml \
  -t vampire-hammer-benchmark:full .
```

Run this image with another build volume. `SAMPLE_SIZE` and `JOBS` also set image
build defaults. Runtime flags override them. `--seed` controls reproducible
sampling, and repeatable `--prover NAME` restricts the experiment's runs.

Mount an experiment XML to select it without rebuilding the image:

```sh
docker run --rm --privileged --cap-drop=ALL --cgroupns=private --network none \
  --mount type=volume,src=vampire-hammer-build,dst=/var/lib/benchmark \
  --mount type=bind,src="$(pwd)/artifacts/docker-results",dst=/results \
  --mount type=bind,src="$(pwd)/configurations/normal_small.xml",dst=/configuration.xml,readonly \
  vampire-hammer-benchmark:sample \
  --definition /configuration.xml --count 25 --output /results/custom
```

`--prover-definition PATH` selects another prover environment XML at startup.
Mount its referenced XML files too, and allow network access for its initial build.
Relative default definition paths resolve inside the copied checkout; absolute
paths refer to mounted files. Experiment task paths resolve beside their XML;
mount any referenced catalogs or saved selections as well. With no task source,
the image uses its cached Mathlib catalog. `--count` can sample a saved selection.

The Docker permissions above provide BenchExec writable cgroups for enforcing the
XML's resource limits; see the [BenchExec container instructions](https://github.com/sosy-lab/benchexec/blob/main/doc/benchexec-in-container.md).
Do not add `--init`: startup replaces itself with BenchExec, preserving its exit
status and keeping it the sole container process before workers start.

Host output contains `suite/`, BenchExec XML and zipped logs under `results/`,
and `benchexec-command.json`. For normalized theorem results:

```sh
python3 scripts/collect-results.py artifacts/docker-results/large-1000/results \
  --suite artifacts/docker-results/large-1000/suite \
  --jsonl artifacts/docker-results/large-1000.jsonl
```

Runtime defaults can also be set with `BENCHMARK_COUNT`, `BENCHMARK_JOBS`,
`BENCHMARK_BUILD_JOBS`, `BENCHMARK_SEED`, `BENCHMARK_OUTPUT`, `BENCHMARK_DEFINITION`
and `BENCHMARK_PROVERS`. Use `--extract-only` to export a suite without benchmarking,
`--benchexec-arg=ARGUMENT` for additional BenchExec flags, or `--help` for options.
