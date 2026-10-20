# Proof-derived premise evaluation

`make-suite.py --strategy lean-auto-style` evaluates VampireHammer with premise
names extracted from human proofs. It replays the source to the environment
immediately before each declaration, so the target and later declarations
are unavailable.

This follows the premise and environment methodology of the lean-auto
experiment; the current dataset, prover and hardware may differ.
The strategy itself runs VampireHammer. For Duper baselines, use
[comparison suites](comparison.md).

## Generate matched FO and HO suites

First [build the mathlib harness](README.md). From the repository root:

```sh
python3 slurmrunner/make-suite.py --backends artifacts/provers/backends.json \
  --prover vampire-hammer --strategy lean-auto-style --count 100 \
  --case-option vampire.parallel=false --case-option vampire.routedSlices=false \
  --case-option vampire.higherOrder=false --output ../lean-auto-style-fo
python3 slurmrunner/make-suite.py --backends artifacts/provers/backends.json \
  --prover vampire-hammer --strategy lean-auto-style --count 100 \
  --catalog ../lean-auto-style-fo/catalog.jsonl \
  --case-option vampire.parallel=false --case-option vampire.routedSlices=false \
  --case-option vampire.higherOrder=true --output ../lean-auto-style-ho
```

Omit `--count` from both commands for the full catalog. Run the suites
separately using the [benchmark runner](README.md). Use `--names` to select existing declarations, or reuse a catalog generated
with `--strategy lean-auto-style`.

The default success cutoff is 10 measured proof seconds with heartbeats
disabled. Imports and source replay are excluded from proof timing.
`proof_timeout` is a failure even if the kernel accepted a late proof.
Same-command dependencies are `excluded` and removed from the eligible
denominator; replay and infrastructure errors remain failures.

Suite configs record revisions and methodology. Result rows retain premise
names, kernel and target-reuse checks, proof time and eligibility.
