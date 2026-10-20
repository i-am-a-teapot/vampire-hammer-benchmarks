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
Use a fresh output directory; existing environments are never overwritten.
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
With `--build`, every project runs the same Lake update, cache, build and smoke
check sequence. The registry records source pins and definition hashes.

Keep case options in experiment files such as `normal.xml`. Prover definitions
and the generated build registry contain no case options. After changing a pin
or wrapper, prepare a fresh environment and regenerate execution suites.
