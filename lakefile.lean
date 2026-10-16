import Lake
open Lake DSL

package vampire_hammer_benchmark where
  version := v!"0.1.0"

-- Use the sibling development checkout. Replace this with a pinned Git
-- dependency when vampire-hammer-clean is published.
require vampire_hammer from "../vampire-hammer-clean" with
  (({} : Lean.NameMap String).insert `buildVampire ((get_config? buildVampire).getD "true"))
require mathlib from git "https://github.com/leanprover-community/mathlib4" @ "v4.34.1"
require Duper from git "https://github.com/leanprover-community/duper" @
  "9e09cd05fd541d6cef4c485882b7978d32c7e0a3"
require aesop from git "https://github.com/leanprover-community/aesop" @
  "355695d523e41d0554926416cba2a2b3544fbbc9"

-- Interpreted harnesses can call lean-auto's non-native evaluation helpers.
@[default_target]
lean_lib VampireHammerBenchmark
