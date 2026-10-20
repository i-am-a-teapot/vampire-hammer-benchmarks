#!/usr/bin/env python3
"""Create independent Lake projects for the common on-the-fly benchmark runner."""
import argparse
import json
import os
from pathlib import Path
import subprocess

PROJECT = Path(__file__).resolve().parents[1]
HAMMER_REV = '93e0c0c4d8121aa2d8ec0ff2a4d54f44d131c85a'
DUPER_REV = '9e09cd05fd541d6cef4c485882b7978d32c7e0a3'
AESOP_REV = '355695d523e41d0554926416cba2a2b3544fbbc9'
AUTO_REV = '6b2774cde4d1cb7ac4d48ac61c34e950e0588d69'
SMT_REV = '79ef7b3d5e06608d031d75b6d015949b90016875'
SMT_AUTO_REV = '92d628ba41801bd12c9d81f9be12fde97f0ad177'
CVC5_REV = '41e47796d4d15712e6064b064ca5d251a1283955'

# All projects implement the same module and registration interface. Only their
# dependency declarations and tactic builders differ.
BACKENDS = {
    'vampire-hammer': {
        'requires': f'require vampire_hammer from git "https://github.com/vprover/vampire-hammer" @ "{HAMMER_REV}"\n',
        'imports': 'import VampireHammer\n',
        'builder': '''initialize registerBenchmarkTactic "vampire-hammer" fun premises => do
  if !((← getOptions).getBool `benchmark.proofPremises false) then
    return ← `(term| by vampire)
  let terms : Array Term := premises.map fun name => ⟨mkIdent name⟩
  let hints ← terms.mapM fun term => `(Auto.hintelem| $term:term)
  let hints := #[(← `(Auto.hintelem| *))] ++ hints
  `(term| by intros; vampire [$[$hints],*])

initialize benchmarkProofModes.set VampireHammer.Replay.reconstructionModes
''',
        'artifacts': 'vampire',
        'case_options': ['vampire.cache=false', 'vampire.suggestedLemmas=0',
                         'vampire.siblingLemmas=false', 'vampire.omitUnsupportedLocalHypotheses=true',
                         'vampire.alignNestedDefinitions=true'],
    },
    'duper': {
        'requires': f'require Duper from git "https://github.com/leanprover-community/duper" @ "{DUPER_REV}"\n',
        'imports': 'import Duper\n',
        'case_options': ['maxSaturationIteration=1000000000'],
        'builder': '''initialize registerBenchmarkTactic "duper" fun premises => do
  let terms : Array Term := premises.map fun name => ⟨mkIdent name⟩
  `(term| by intros; duper [*, $[$terms],*])
''',
    },
    'aesop': {
        'requires': f'require aesop from git "https://github.com/leanprover-community/aesop" @ "{AESOP_REV}"\n',
        'imports': 'import Aesop\n',
        'builder': '''initialize registerBenchmarkTactic "aesop" fun premises => do
  let clauses ← premises.mapM fun name => do
    let ident := mkIdent name
    `(Aesop.tactic_clause| (add unsafe $ident:ident))
  let config ← `(Aesop.tactic_clause| (config := {
    maxRuleApplications := 0, maxRuleApplicationDepth := 0,
    maxNormIterations := 1000000000, maxSafePrefixRuleApplications := 0 }))
  let clauses := #[config] ++ clauses
  `(term| by intros; aesop $[$clauses]*)
''',
    },
    'simp_all': {'requires': '', 'imports': '', 'builder': ''},
    'grind': {'requires': '', 'imports': '', 'builder': ''},
    'lean-auto-duper': {
        'requires': f'require Duper from git "https://github.com/leanprover-community/duper" @ "{DUPER_REV}"\n'
                    f'require auto from git "https://github.com/leanprover-community/lean-auto" @ "{AUTO_REV}"\n',
        'imports': 'import Duper\nimport Auto.Tactic\n',
        'builder': '''open Lean Auto in
@[rebind Auto.Native.solverFunc]
def Auto.duperRaw (lemmas : Array Lemma) (_inhs : Array Lemma) : MetaM Expr := do
  let formulas : Array (Expr × Expr × Array Name × Bool) ← lemmas.mapM
    (fun ⟨⟨proof, ty, _⟩, _⟩ => do return (ty, ← Meta.mkAppM ``eq_true #[proof], #[], true))
  Duper.runDuper formulas.toList [] 0

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
''',
    },
    'lean-smt': {
        'requires': f'require smt from git "https://github.com/ufmg-smite/lean-smt.git" @ "{SMT_REV}"\n'
                    f'require auto from git "https://github.com/leanprover-community/lean-auto.git" @ "{SMT_AUTO_REV}"\n'
                    f'require cvc5 from git "https://github.com/abdoo8080/lean-cvc5.git" @ "{CVC5_REV}"\n',
        'imports': 'import Smt\nimport Smt.Real\n',
        'builder': '''initialize registerBenchmarkTactic "lean-smt" fun premises => do
  let terms : Array Term := premises.map fun name => ⟨mkIdent name⟩
  let hints ← terms.mapM fun term => `(Smt.Tactic.smtHintElem| $term:term)
  `(term| by intros; smt +mono -trust -native [*, $[$hints],*])
''',
    },
}


def prepare(output, provers, build=False):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    # Preflight all destinations before creating any projects.
    for prover in provers:
        if (output / prover).exists():
            raise ValueError(f'project already exists: {output / prover}')
    registry = {'version': 1, 'backends': {}}
    for prover in provers:
        spec = BACKENDS[prover]
        directory = output / prover
        (directory / 'Benchmark').mkdir(parents=True)
        (directory / 'lean-toolchain').write_text((PROJECT / 'lean-toolchain').read_text())
        (directory / 'lakefile.lean').write_text(
            'import Lake\nopen Lake DSL\npackage benchmark_prover\n'
            f'require vampire_hammer_benchmark from {json.dumps(str(PROJECT))}\n'
            + spec['requires'] + '\n@[default_target]\nlean_lib Benchmark where\n'
            '  roots := #[`Benchmark.Prover]\n')
        (directory / 'Benchmark/Prover.lean').write_text(
            'import VampireHammerBenchmark.Comparison\n' + spec['imports'] +
            '\nnamespace VampireHammer.Benchmark\nopen Lean Elab Term\n\n' +
            spec['builder'] + '\nend VampireHammer.Benchmark\n')
        smoke = (PROJECT / 'slurmrunner/tests/ComparisonBenchmarkTests.lean').read_text()
        smoke = smoke.replace('import VampireHammerBenchmark.Comparison', 'import Benchmark.Prover')
        smoke = smoke.replace('for prover in #["simp_all", "grind"]', f'for prover in #[{json.dumps(prover)}]')
        smoke = smoke.replace('"Tests/Frontend/ComparisonBenchmarkFixture.lean"',
                              json.dumps(str(PROJECT / 'Tests/Frontend/ComparisonBenchmarkFixture.lean')))
        smoke = smoke.replace('    let options := options.setBool',
                              '    let options := options.set `benchmark.adapterModule "Benchmark.Prover"\n'
                              '    let options := options.setBool')
        (directory / 'Smoke.lean').write_text(smoke)
        registry['backends'][prover] = {
            'project': str(directory), 'adapter_module': 'Benchmark.Prover',
            'benchmark_imports': 'import Mathlib\nimport Benchmark.Prover\n',
            'artifacts': spec.get('artifacts'),
            'case_options': spec.get('case_options', []),
        }
    (output / 'backends.json').write_text(json.dumps(registry, indent=2) + '\n')
    if build:
        for prover in provers:
            directory = output / prover
            for command in (['lake', 'update'], ['lake', 'exe', 'cache', 'get'],
                            ['lake', 'build'], ['lake', 'lean', 'Smoke.lean']):
                environment = {key: value for key, value in os.environ.items()
                               if key not in {'LEAN_PATH', 'LEAN_SRC_PATH', 'LEAN_SYSROOT'}}
                subprocess.run(command, cwd=directory, env=environment, check=True)
    return output / 'backends.json'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--prover', action='append', choices=BACKENDS,
                        help='repeat to select provers; default: all supported projects')
    parser.add_argument('--build', action='store_true', help='update, fetch Mathlib caches, and build every selected project')
    args = parser.parse_args()
    try:
        registry = prepare(args.output, list(dict.fromkeys(args.prover or BACKENDS)), args.build)
    except ValueError as error:
        parser.error(str(error))
    print(f'Prepared isolated prover projects. Registry: {registry}')
    if not args.build:
        print('In each project run: lake update && lake exe cache get && lake build && lake lean Smoke.lean')


if __name__ == '__main__':
    main()
