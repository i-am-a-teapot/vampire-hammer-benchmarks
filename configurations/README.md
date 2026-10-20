# Prover environments and experiments

`provers.xml` selects the six prover definitions in `provers/`. `normal.xml`
selects testcases, budgets and case options for the standard experiment.

```sh
python3 scripts/prepare-provers.py --definition configurations/provers.xml \
  --output artifacts/provers --build
python3 scripts/make-suite.py --definition configurations/normal.xml \
  --backends artifacts/provers/backends.json --format slurm --output artifacts/suite
```

Omit `--build` to generate projects without fetching or compiling dependencies.
Rerun the same command to refresh generated files and resume an interrupted build.
Unchanged files retain their timestamps; dependency checkouts, build caches and
unselected provers are preserved. Replaced shared-package directories are archived
under the project's `.lake/previous-packages/`.
Repeat `--prover NAME` to select a subset. A single prover XML can also be passed
directly to `--definition`.

## Add or change a prover

Each file defines a name, a project directory relative to the preparation output,
its pinned Lake dependencies, imports and a Lean wrapper. For example:

```xml
<prover name="duper" checkout="duper">
  <dependency name="Duper"
    git="https://github.com/leanprover-community/duper"
    commit="9e09cd05fd541d6cef4c485882b7978d32c7e0a3"/>
  <imports><![CDATA[
import Duper
]]></imports>
  <wrapper><![CDATA[
fun premises => do
  let terms : Array Term := premises.map fun name => ⟨mkIdent name⟩
  `(term| by intros; duper [*, $[$terms],*])
]]></wrapper>
</prover>
```

Add `<prover file="provers/NAME.xml"/>` to the environment index. File paths
resolve beside the XML containing them. Git dependencies require full commit
hashes; Lake fetches them into each generated project's `.lake/packages/`.
An optional dependency `path="..."` uses an existing local Lake checkout instead,
checking its HEAD against `commit` and rejecting modified tracked files.

The wrapper is a Lean expression of type `Array Name → TermElabM Term`: it receives
proof-derived premise names and returns the term used to prove the target.
The generator registers it under the XML prover name. Imports precede the
`VampireHammer.Benchmark` namespace; optional `<declarations>` provides helper
code inside that namespace, before registration. The generated module opens
`Lean Elab Term`. CDATA keeps Lean quotation syntax readable.

Use `artifacts="vampire"` for the standard Vampire-Hammer solver artifacts.
Mathlib, its dependency pins (including Aesop), native caches and the Lean
toolchain come from the root project. Explicit dependencies matching those
shared packages must keep the same Git URL and commit.

Preparation writes `backends.json`, definition snapshots and generated
`Benchmark/Prover.lean`, `lakefile.lean`, `lean-toolchain`, and `Smoke.lean` files.
With `--build`, each selected project runs Lake cache, build and smoke checks.
Add `--no-cache` to compile locally without Mathlib or Lake cache downloads,
including Mathlib's automatic cache hook during dependency updates.
`--build-jobs N` limits Lean build threads and native Vampire compile jobs.
Lake updates dependencies when the lockfile is missing or its pins differ.
A first-time Vampire build that finishes compiling but fails because Lake retained
the initial empty-checkout HEAD probe error is retried once with the initialized
checkout. Other build failures remain errors. The registry records source pins
and definition hashes.

Keep case options in experiment files such as `normal.xml`. Prover definitions
and the generated build registry contain no case options. After changing a pin
or wrapper, rerun preparation and regenerate execution suites.
