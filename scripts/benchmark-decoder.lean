import VampireHammer.Replay.Decode
import VampireHammer.TSTP

/-! Synthetic decoder benchmarks. Run from vampire_hammer with:
lake lean scripts/benchmark-decoder.lean

Parsing is outside the timed region. Each case runs three times and prints
`case,size,trial,nanoseconds`. These measure decoder operations, not Vampire
search or kernel checking. Artifacts use the regular ignored directory. -/

open VampireHammer Auto.Embedding.Lam

set_option maxRecDepth 100000
set_option maxHeartbeats 0

@[noinline] private def normalizeAndCheck (term : LamTerm) (expected : Nat) : IO Unit := do
  let normalized := Replay.Decode.Internal.normalizeSourceOr { term, sort := .base .prop }
  -- Checking inside the IO function forces evaluation before the end timer.
  unless (Replay.Internal.flattenOr normalized.term).length == expected do
    throw <| IO.userError "lost disjuncts"

#eval show IO Unit from do
  let directory : System.FilePath := "artifacts/decoder-benchmark"
  IO.FS.createDirAll directory
  let library := ""
  let problem : TFF.Problem := {
    text := "", sorts := #[], facts := #[],
    symbols := (List.range 4000).toArray.map fun index =>
      (s!"p{index}", .atom index, .base .prop) }
  let path := directory / "chain.tstp"
  for size in [500, 1000, 2000, 4000] do
    let text := String.join <| (List.range size).map fun index =>
      if index + 1 == size then s!"tff(f{index},axiom,p3999,file('input',fact0)).\n"
      else s!"tff(f{index},plain,p3999,inference(rewrite,[],[f{index + 1}])).\n"
    IO.FS.writeFile path text
    let proof ← IO.ofExcept (← parseVampireTSTP library path.toString)
    for trial in [:3] do
      let start ← IO.monoNanosNow
      let decoded ← IO.ofExcept (Replay.decodeSteps problem proof)
      unless decoded.steps.size == size do throw <| IO.userError "lost proof nodes"
      let nanos := (← IO.monoNanosNow) - start
      IO.println s!"decode,{size},{trial},{nanos}"
  for size in [100, 500, 1000, 2000] do
    let term := ((List.range size).map LamTerm.atom).foldl LamTerm.mkOr (.atom size)
    for trial in [:3] do
      let start ← IO.monoNanosNow
      normalizeAndCheck term (size + 1)
      let nanos := (← IO.monoNanosNow) - start
      IO.println s!"normalize,{size},{trial},{nanos}"
