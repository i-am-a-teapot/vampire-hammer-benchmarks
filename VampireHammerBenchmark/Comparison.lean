import VampireHammerBenchmark.Mathlib

/-! Lean's built-in tactics. External provers register their builders from an
isolated project's Benchmark.Prover module; they are never imported together. -/

namespace VampireHammer.Benchmark
open Lean Elab Term

initialize registerBenchmarkTactic "rfl" fun _ => `(term| by intros; rfl)

initialize registerBenchmarkTactic "simp_all" fun premises => do
  let terms : Array Term := premises.map fun name => ⟨mkIdent name⟩
  `(term| by intros; simp_all [$[$terms:term],*])

initialize registerBenchmarkTactic "grind" fun premises => do
  let terms : Array Term := premises.map fun name => ⟨mkIdent name⟩
  `(term| by intros; grind [$[$terms:term],*])

end VampireHammer.Benchmark
