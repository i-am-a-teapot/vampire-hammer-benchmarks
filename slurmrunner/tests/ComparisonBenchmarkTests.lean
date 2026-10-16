import VampireHammerBenchmark.Comparison

open Lean Meta Elab

run_elab do
  let directory := System.FilePath.mk "artifacts/comparison-benchmark-tests"
  IO.FS.createDirAll directory
  let source := System.FilePath.mk "Tests/Frontend/ComparisonBenchmarkFixture.lean"
  for prover in #["vampire-hammer", "duper", "aesop", "simp_all", "grind"] do
    let options := (← getOptions).set `maxHeartbeats (0 : Nat)
    let options := options.set `vampire.benchmarkProver prover
    let options := options.setBool `vampire.benchmarkComparison true
    for (target, expected) in #[(`ComparisonBenchmarkFixture.target, "proved"),
                                (`ComparisonBenchmarkFixture.excluded, "excluded")] do
      let path := directory / s!"{prover}-{target}.jsonl"
      IO.FS.writeFile path ""
      withOptions (fun _ => options) <|
        VampireHammer.Benchmark.replayDeclaration target source path.toString
      let events ← (← IO.FS.lines path).mapM fun line => do
        match Json.parse line with
        | .ok event => pure event
        | .error error => throwError "invalid benchmark event: {error}"
      let some finished := events.find? (fun event =>
          event.getObjValAs? String "event" == .ok "finished")
        | throwError "{prover} did not finish"
      unless finished.getObjValAs? String "status" == .ok expected do
        throwError "{prover}: unexpected result {finished.compress}"
      if expected == "proved" then
        unless finished.getObjValAs? Bool "kernel_checked" == .ok true &&
            finished.getObjValAs? Bool "reuses_target" == .ok false &&
            finished.getObjValAs? Bool "target_in_environment" == .ok false do
          throwError "{prover}: proof was not independently kernel checked"
        unless finished.getObjValAs? (Array String) "premises" ==
            .ok #["ComparisonBenchmarkFixture.premise"] do
          throwError "{prover}: wrong premise list"
        unless events.any (fun event => event.getObjValAs? String "event" == .ok "proof_checked") do
          throwError "{prover}: no kernel-check timing boundary"
      else
        unless finished.getObjValAs? String "exclusion_reason" == .ok "same_command_dependency" do
          throwError "{prover}: same-command filter was not applied"
