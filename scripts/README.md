# Benchmark configuration and execution

All testcases use lean-auto-style evaluation: proof-derived premises and source
replay before the target declaration. The default proof-success and search
budgets are 10 seconds. See the [build instructions](../README.md).

## Select testcases once

```sh
python3 scripts/make-suite.py --tasks-only --count 100 --seed experiment-1 \
  --output artifacts/mathlib-cases
```

This extracts a catalog using the built root project, without prover projects.
Use `--catalog PATH` to select from an existing proof-derived catalog instead.
Omit `--count` for the full catalog, or use `--names FILE` for specific theorems.
The output stores names, modules and IDs in `selection.jsonl`, plus Mathlib
revision and methodology in `test-suite.json`. It has no prover settings.
Premise extraction and source replay happen during execution.

## Define prover configurations

Save this as `artifacts/experiment.xml`; paths resolve beside the XML file:

```xml
<benchmark timelimit="600 s" memlimit="8 GB">
  <tasks>
    <option name="--test-suite">mathlib-cases</option>
  </tasks>
  <rundefinition name="duper-default">
    <option name="--prover">duper</option>
  </rundefinition>
  <rundefinition name="duper-tuned">
    <option name="--prover">duper</option>
    <option name="--case-option">maxSaturationIteration=1000000</option>
  </rundefinition>
  <rundefinition name="hammer">
    <option name="--prover">vampire-hammer</option>
    <option name="--search-seconds">10</option>
  </rundefinition>
</benchmark>
```

Root `<option>` elements can supply shared `--case-option` and `--search-seconds`
values. Run options follow shared options; the last Lean option value wins.
Repeat search budgets for multiple runs. Unsupported Lean options are omitted
using each build's registered options and recorded in `omitted_case_options`.
Protocol heartbeat limits remain zero. Run names identify separate configs and
result groups, even for the same prover.

`<tasks>` also accepts `--catalog`, `--names`, `--count` and `--seed`.
Combine `--test-suite` with `--count` and `--seed` to sample a saved selection;
testcase IDs are preserved. `--catalog` and `--test-suite` are alternative sources.
Root attributes accept `success-seconds`, `hardtimelimit`,
`walltimelimit` and `cpu-cores`. CLI selection values and process limits override
XML values. This is a BenchExec-inspired input format, not a general XML importer.

## Generate and run

```sh
python3 scripts/make-suite.py --definition artifacts/experiment.xml \
  --backends artifacts/provers/backends.json --format slurm --output artifacts/suite
python3 slurmrunner/slurmbench.py run-local artifacts/suite/vampire-hammer.xml \
  --jobs 4 --batch-by-module 16 -o artifacts/results
```

Generation reuses the selection and checks matching Mathlib revisions.
Each named config records build fingerprints, effective and omitted options;
`experiment-input.xml` preserves the input. Any generated suite can itself be
reused with `--test-suite`. Use `--prover NAME` to select matching XML runs.
The task runner requires a generated isolated prover config; unconfigured builds
and older configs without recorded option capabilities are unsupported.

For Slurm submission and collection, see the [root README](../README.md).
`collect --partial` reports progress; `resubmit-shard MANIFEST SHARD` resumes
missing work. Report full-corpus rates only after complete collection.
Per-theorem proof seconds are the timing metric; batched CPU/RSS measurements
belong to the batch.

## Export for BenchExec

`make-suite.py` defaults to `--format benchexec`. This produces standard XML
and YAML task definitions, with one theorem input per task. Use the same
experiment and prover registry as for Slurm:

```sh
python3 scripts/make-suite.py --definition configurations/normal.xml \
  --backends artifacts/provers/backends.json --format benchexec \
  --output artifacts/benchexec-suite
PYTHONPATH="$(pwd)/scripts${PYTHONPATH:+:$PYTHONPATH}" benchexec \
  --no-container --tool-directory "$(pwd)" \
  -o artifacts/benchexec-results/ artifacts/benchexec-suite/vampire-hammer.xml
python3 scripts/collect-results.py artifacts/benchexec-results \
  --suite artifacts/benchexec-suite --jsonl artifacts/benchexec-results.jsonl
```

BenchExec is an optional dependency (`python3 -m pip install benchexec`) and
requires [Linux cgroup setup](https://github.com/sosy-lab/benchexec/blob/main/doc/INSTALL.md).
The command above uses the
host filesystem so recorded project and toolchain paths are available.
`--format slurm` instead writes the compact JSONL catalog for Slurm/local runs.
The Slurm runner also accepts the standard BenchExec export. Both formats use
the same prover configs and proof checks. Each script provides `--help`.
