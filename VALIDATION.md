# Isolated prover validation

The isolated-project refactor was checked with Lean 4.34.1 and Python 3.14:

- All 101 Python tests and both shared-core Lean regressions pass.
- The core builds without Vampire-Hammer, Duper or lean-auto dependencies.
- Independent `simp_all` and Duper projects build and pass their replay smoke
  tests, including premise extraction, kernel checking and same-command exclusion.
- Duper resolves upstream lean-auto at `6b2774cde4d1cb7ac4d48ac61c34e950e0588d69`;
  the `simp_all` environment has no lean-auto module path.
- The local entry point generates one suite for both backends, batches source
  replay by module, and collects all four results for two Mathlib theorems
  through the same worker and collector used by Slurm. Proof successes and
  unsuccessful tactic attempts remain separate outcomes.
- Python regressions cover all seven project templates, equal build sequences,
  per-backend Slurm configurations, module-path isolation, stale-source checks,
  subset comparison denominators, local execution and resume.

No Slurm jobs were submitted. The remaining native solver environments were
not rebuilt as part of this refactor; their generated adapters use the existing
pinned dependencies and tactic configurations.

# Historical extraction validation

Validated with Lean 4.34.1 and Python 3.14 against an isolated working-source
copy of `vampire-hammer-clean` and its clean pinned Lean-auto dependency.
The existing certificate-emitting Vampire 5.1.0 binary reporting commit
`6ee1f12` was supplied explicitly, avoiding a new native prover build.

- The standalone `VampireHammerBenchmark` Lake library builds successfully.
- All 104 Python tests pass, including local dependency/parser resolution,
  unaveraged CSV export, isolated baseline generation, suite snapshots,
  execution budgets, result collection, repetitions, Slurm staging, and plots.
- Both Lean benchmark regressions pass: premise extraction/dependency auditing
  and declaration replay/comparison. The comparison fixture passes for all
  five directly registered tactics.
- A compact one-theorem comparison suite is generated with source hashes,
  source archive, runtime/native-library flags, and five tactic tasks.
- The runner correctly records the same-command exclusion of `Finset.image_id`.
- On `Finset.mem_union` in its actual defining module
  `Mathlib.Data.Finset.Lattice.Basic`, Vampire-Hammer and Duper produce
  kernel-checked proofs. Aesop's reconstructed result containing `sorryAx`
  is rejected; `simp_all` and `grind` report tactic failures. These are retained
  as unsuccessful outcomes, rather than counted as proofs.
- The two published CSVs retain the original bytes and their 196,083 theorem
  rows per run; their successful counts were checked against the original XML
  result sets before extraction. See `data/SHA256SUMS`.

The optional upstream Lean-auto and Lean-SMT project generators are exercised;
this extraction does not build a new native cvc5 FFI environment, launch Slurm
jobs, or rerun the full mathlib experiment. Historical CSV results describe
those original runs, rather than this new smoke experiment.

The validation build caches and temporary test suites are not part of the
repository. Follow README.md to prepare a new benchmark build.
