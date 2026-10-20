# Docker

The image consumes the repositories already compiled by the normal build commands:

```sh
lake update
lake exe cache get
lake build
python3 scripts/prepare-provers.py --definition configurations/provers.xml \
  --output artifacts/provers --build
```

These commands put shared compiled dependencies in the checkout's `.lake/` and
prover-specific builds in `artifacts/provers/`. Supply both, together with the
matching sources and manifests. An `artifacts/provers/` directory alone is not
sufficient. Keep package Git metadata and the shared dependency symlinks.
Build artifacts must be from Linux x86-64 with system libraries compatible with
Ubuntu 24.04; builds produced on macOS or ARM64 cannot be used.

Prepared prover projects use relative paths in their Lake dependencies,
registry, and shared-package symlinks.

Stage the prepared checkout and its pinned Lean toolchain in a separate artifacts
directory, excluding local credentials and unrelated benchmark output. From the
checkout used to compile:

```sh
mkdir -p artifacts/docker-precompiled/checkout artifacts/docker-precompiled/toolchain
precompiled_dir="$(cd artifacts/docker-precompiled && pwd -P)"
cp -a lean-toolchain lakefile.lean lake-manifest.json LICENCE \
  VampireHammerBenchmark.lean VampireHammerBenchmark scripts slurmrunner Tests configurations docs \
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

