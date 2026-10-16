#!/usr/bin/env python3
"""Create an isolated upstream lean-auto project using the shared replay harness."""
import argparse
import hashlib
import json
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
AUTO_REV = '6b2774cde4d1cb7ac4d48ac61c34e950e0588d69'


def harness_source(source):
    # Only remove hammer-specific tactic construction and proof attribution.
    # Premise extraction, replay, timing, kernel checking and audits stay shared.
    source = source.replace('import VampireHammer\n', 'import Auto.Tactic\n')
    start = source.index('    else if useProofPremises then do\n', source.index('let tactic : Term'))
    end = source.index('    appendEvent path', source.index('      `(term| by vampire)\n', start))
    source = source[:start] + '    else\n      throwError "VampireHammer is unavailable in the upstream baseline project"\n' + source[end:]
    source = source.replace('let reconstructionModes := VampireHammer.Replay.reconstructionModes proof',
                            'let reconstructionModes : Array String := #[]')
    source = source.replace('namespace VampireHammer.Benchmark\n', '''namespace VampireHammer.Benchmark

register_option vampire.outputPath : String := {
  defValue := ""
  descr := "Unused compatibility option for the shared replay harness"
}
''', 1)
    return source


COMPARISON = '''import VampireHammerBenchmark.Mathlib
import Duper
import Aesop

-- The native solver adapter documented by upstream lean-auto's README.
open Lean Auto in
@[rebind Auto.Native.solverFunc]
def Auto.duperRaw (lemmas : Array Lemma) (_inhs : Array Lemma) : MetaM Expr := do
  let formulas : Array (Expr × Expr × Array Name × Bool) ← lemmas.mapM
    (fun ⟨⟨proof, ty, _⟩, _⟩ => do return (ty, ← Meta.mkAppM ``eq_true #[proof], #[], true))
  Duper.runDuper formulas.toList [] 0

namespace VampireHammer.Benchmark
open Lean Elab Term

initialize registerBenchmarkTactic "lean-auto-duper" fun premises => do
  let terms : Array Term := premises.map fun name => ⟨mkIdent name⟩
  let hints ← terms.mapM fun term => `(Auto.hintelem| $term:term)
  let hints := #[(← `(Auto.hintelem| *))] ++ hints
  `(term| by
    intros
    set_option auto.native true in
      set_option auto.smt false in
        set_option auto.tptp false in
          auto [$[$hints],*])

end VampireHammer.Benchmark
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    harness = PROJECT / 'VampireHammerBenchmark/Mathlib.lean'
    directory = output / 'VampireHammerBenchmark'
    directory.mkdir(parents=True)
    (directory / 'Mathlib.lean').write_text(harness_source(harness.read_text()))
    (directory / 'Comparison.lean').write_text(COMPARISON)
    (output / 'lean-toolchain').write_text((PROJECT / 'lean-toolchain').read_text())
    (output / 'lakefile.lean').write_text(f'''import Lake
open Lake DSL
package upstream_auto_benchmark
require auto from git "https://github.com/leanprover-community/lean-auto" @ "{AUTO_REV}"
require mathlib from git "https://github.com/leanprover-community/mathlib4" @ "v4.34.1"
require Duper from git "https://github.com/leanprover-community/duper" @ "9e09cd05fd541d6cef4c485882b7978d32c7e0a3"
require aesop from git "https://github.com/leanprover-community/aesop" @ "355695d523e41d0554926416cba2a2b3544fbbc9"
@[default_target]
lean_lib UpstreamBenchmark where
  roots := #[`VampireHammerBenchmark.Mathlib, `VampireHammerBenchmark.Comparison]
''')
    (output / 'upstream-benchmark.json').write_text(json.dumps({
        'auto_revision': AUTO_REV,
        'harness_sha256': hashlib.sha256(harness.read_bytes()).hexdigest(),
    }, indent=2) + '\n')
    print(f'Created {output}; run lake update, lake exe cache get, and lake build there.')


if __name__ == '__main__':
    main()
