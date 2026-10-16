#!/usr/bin/env python3
"""Create an isolated Lean-SMT/cvc5 project with the shared benchmark harness."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
SMT_REV = '79ef7b3d5e06608d031d75b6d015949b90016875'
AUTO_REV = '92d628ba41801bd12c9d81f9be12fde97f0ad177'
CVC5_REV = '41e47796d4d15712e6064b064ca5d251a1283955'

spec = importlib.util.spec_from_file_location('prepare_auto', PROJECT / 'slurmrunner/prepare-lean-auto.py')
preparation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preparation)


def harness_source(source):
    # A module interface keeps the tactic's mathlib imports in the meta layer
    # when replaying a declaration-time module environment.
    source = preparation.harness_source(source)
    source = source.replace('imports.push { module := benchmarkModule, isMeta := true }',
                            'imports.push { module := benchmarkModule, isMeta := true, isExported := false }')
    source = source.replace('  let replayStart ← IO.monoNanosNow\n', '''  -- Solver imports may already contain foundational mathlib targets, even
  -- when visible only in the meta layer. Never replay or benchmark them.
  let mut remainingTargets := #[]
  for target in targets do
    if ((env.setExporting false).find? target).isSome then
      appendEvent path <| Json.mkObj [
        ("event", toJson "finished"), ("name", toJson target.toString),
        ("status", toJson "excluded"),
        ("exclusion_reason", toJson "baseline_import_dependency"),
        ("kernel_checked", toJson false)]
    else
      remainingTargets := remainingTargets.push target
  let targets := remainingTargets
  if targets.isEmpty then return #[]
  let replayStart ← IO.monoNanosNow
''')
    lines = source.splitlines(keepends=True)
    imports = []
    while lines and lines[0].startswith('import '):
        line = lines.pop(0)
        imports.extend(['public ' + line, 'public meta ' + line])
    return 'module\n' + ''.join(imports) + '\npublic meta section\n' + ''.join(lines)


COMPARISON = '''module
public import VampireHammerBenchmark.Mathlib
public meta import VampireHammerBenchmark.Mathlib
public meta import Smt
public meta import Smt.Real

public meta section

namespace VampireHammer.Benchmark
open Lean Elab Term

initialize registerBenchmarkTactic "lean-smt" fun premises => do
  let terms : Array Term := premises.map fun name => ⟨mkIdent name⟩
  let hints ← terms.mapM fun term => `(Smt.Tactic.smtHintElem| $term:term)
  `(term| by
    intros
    smt +mono -trust -native [*, $[$hints],*])

end VampireHammer.Benchmark
'''

SMOKE = '''import VampireHammerBenchmark.Comparison

set_option maxHeartbeats 0
set_option synthInstance.maxHeartbeats 0

theorem smtBenchmarkImplication (p q : Prop) (hp : p) (hpq : p → q) : q := by
  smt +mono -trust -native [*]

theorem smtBenchmarkArithmetic (x : Int) (h : x > 5) : x > 0 := by
  smt +mono -trust -native [*]

#print axioms smtBenchmarkImplication
#print axioms smtBenchmarkArithmetic
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False)
    harness = PROJECT / 'VampireHammerBenchmark/Mathlib.lean'
    directory = output / 'VampireHammerBenchmark'
    directory.mkdir(parents=True)
    (directory / 'Mathlib.lean').write_text(harness_source(harness.read_text()))
    (directory / 'Comparison.lean').write_text(COMPARISON)
    (output / 'Smoke.lean').write_text(SMOKE)
    (output / 'lean-toolchain').write_text((PROJECT / 'lean-toolchain').read_text())
    (output / 'lakefile.lean').write_text(f'''import Lake
open Lake DSL
package lean_smt_benchmark
require smt from git "https://github.com/ufmg-smite/lean-smt.git" @ "{SMT_REV}"
require auto from git "https://github.com/leanprover-community/lean-auto.git" @ "{AUTO_REV}"
require cvc5 from git "https://github.com/abdoo8080/lean-cvc5.git" @ "{CVC5_REV}"
require mathlib from git "https://github.com/leanprover-community/mathlib4" @ "v4.34.1"
@[default_target]
lean_lib SmtBenchmark where
  roots := #[`VampireHammerBenchmark.Mathlib, `VampireHammerBenchmark.Comparison]
''')
    (output / 'upstream-benchmark.json').write_text(json.dumps({
        'smt_revision': SMT_REV, 'auto_revision': AUTO_REV, 'cvc5_revision': CVC5_REV,
        'harness_sha256': hashlib.sha256(harness.read_bytes()).hexdigest(),
    }, indent=2) + '\n')
    print(f'Created {output}; run lake update, lake exe cache get, lake build, and lake lean Smoke.lean there. '
          'cvc5 is provided by the Lean FFI dependency, not a PATH executable.')


if __name__ == '__main__':
    main()
