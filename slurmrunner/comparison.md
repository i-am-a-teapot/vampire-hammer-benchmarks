# Isolated prover projects

The supported comparison workflow uses one Lake project per prover:

```sh
python3 slurmrunner/prepare-provers.py --output artifacts/provers --build
python3 slurmrunner/make-suite.py --backends artifacts/provers/backends.json \
  --comparison --compact --count 100 --output ../comparison-isolated
```

Repeat `--prover NAME` to select projects when preparing or generating a suite.
The projects share the benchmark harness, Mathlib and its pinned dependency
checkouts/build caches. Solver-specific packages such as `auto` remain separate.
Environment replay, premise selection,
kernel check, target-reuse audit and Slurm runner remain shared. Backend imports
can differ; targets already imported by a tactic are excluded uniformly.

# Tactic comparisons

`make-suite.py --backends REGISTRY --comparison` compares the selected backends.
Each tactic receives the same direct theorem dependencies and local hypotheses in the environment before the target
declaration. The original proof supplies premise names and eligibility only.

The common default is a 10-second proof budget, including premise extraction,
tactic execution and kernel checking. Imports, source replay and dependency
auditing are outside that budget but inside the 600-second process limit. This
longer limit prevents an unusually expensive declaration replay from being
misreported as a tactic timeout. Heartbeats
are disabled for the overall Lean command (`maxHeartbeats=0`) and typeclass
synthesis (`synthInstance.maxHeartbeats=0`) for every tactic. Internal windows
remain unchanged, including lean-auto's monomorphization unification limits
and VampireHammer's preprocessing/fallback windows (`vampire.monoHeartbeats`).
Late proofs are failures; successful proofs must pass the kernel
and target-reuse/`sorryAx` audit. Same-command dependency exclusions are
removed from the eligible denominator.

## Prepare and run

From the repository root:

```sh
lake -R update
lake -R exe cache get
lake -R build VampireHammerBenchmark
python3 slurmrunner/prepare-provers.py --output artifacts/provers --build

python3 slurmrunner/make-suite.py --backends artifacts/provers/backends.json \
  --comparison --compact --count 12 \
  --search-seconds 10 --success-seconds 10 --output ../comparison-pilot
python3 slurmrunner/slurmbench.py submit-packed ../comparison-pilot/vampire-hammer.xml \
  -p PARTITION --nodes 1 --cpus-per-node 4 --mem 32G --shards 1 \
  --repetitions 3 --batch-by-module 16 \
  -o ../comparison-results
python3 slurmrunner/slurmbench.py collect ../comparison-results/manifest.json
```

Omit `--count` for the full catalog. Use new output directories and adjust
resources for your cluster. For an FO-only or HO-only VampireHammer run, add
`--case-option vampire.routedSlices=false` and
`--case-option vampire.higherOrder=false` or `true`.

## Solver dependencies and eligibility

`prepare-provers.py` prepares `vampire-hammer`, `duper`, `aesop`, `simp_all`,
`grind`, and `lean-auto-duper`. Select a subset with repeatable
`--prover NAME` arguments. All projects use the same preparation and build
sequence, and each adapter imports only its own tactic dependencies.

Suite generation verifies matching Lean and Mathlib versions and records each
backend's dependency revisions, imports, adapter and runtime configuration.
Theorems already imported by any tactic are excluded as
`baseline_import_dependency`, including targets present only in the meta layer.
The common eligible comparison excludes these cases for every tool.

Collection writes `comparison-search10.json` and `.tsv`, including common
eligible, available-subset and pairwise results. Use `--partial` for progress;
publish full-corpus rates only after a complete collection.

`--batch-by-module 16` amortizes startup within one module and tactic.
Per-theorem proof seconds are the timing metric; CPU/RSS values belong to the
batch. Packed mode does not enforce per-execution memory limits.
`--require-no-boost-no-smt` requires verified machine settings at startup
and batch boundaries.

## Plot and audit collected comparisons

To audit which export routes occur in accepted vampire-hammer proofs, including
evidence of decomposition and branch searches, run:

```sh
python3 slurmrunner/analyze-routes.py \
  ~/comparison-full-no-bundled-results ~/comparison-full-no-bundled-results2 \
  --output artifacts/comparison-full-no-bundled-average/routes
```

This produces `report.md`, `summary.json`, and per-theorem CSVs for the
vampire-hammer population and both common comparison populations. FO/HO counts
come from reconstruction markers in the accepted proof, rather than the last
solver invocation. Mixed proofs can arise from either structural decomposition
or inhabitation case analysis. Decomposition events and branch problem artifacts
are counted separately: existing logs do not mark accepted structural splits,
and cannot identify splits whose branches all close directly in Lean. The
report preserves these limitations rather than attributing all mixed proofs or
all entries into the decomposition fallback to successful splitting.

`slurmrunner/analyze-empty-cases.py` accepts the same directories and `--output`
argument to audit empty-domain recovery, including failed benchmark cases. It
produces per-case CSVs, residual-branch event traces and a JSON summary. An
`empty_branch` event means simplification left an obligation; external calls
within the recovery are tracked separately. Existing events cannot distinguish
an unused nonempty assumption from an empty case closed by the initial
simplification. Failed recovery attempts survive rollback and are flagged as
ambiguous when counting completed branches.

With Python 3.10+ and Matplotlib installed, run from the repository root:

```sh
python3 slurmrunner/plot-comparison.py ~/comparison-full-no-bundled-results \
  --output artifacts/comparison-full-no-bundled
```

The output includes an HTML report (`index.html`), Markdown and CSV tables,
lin-log and linear cactus plots in PDF/SVG/PNG, and their sorted timing
CSVs. The x-axis counts solved theorems and the y-axis shows each theorem's
proof time (`hammer-seconds`), rather than cumulative time or batch resources.
The main plots (`cactus-linlog.*`) use a linear theorem-count axis
and a logarithmic proof-time axis; `cactus-linear.*` provides the corresponding linear view.
Publication exports use serif typography, a color-vision-friendly Tol palette,
distinct line patterns, an adjacent scriptsize table, embedded PDF fonts, and
300 dpi PNGs. Plot times are displayed in
seconds. Tool names use small caps, while Theoretical best uses the normal serif
font. The table beside the plot identifies the curves and reports solved counts.
The total number of matched eligible benchmark instances appears above the table.
The “Unique” column counts solves absent from every other real solver in that
comparison, computed per run before averaging. The theoretical portfolio is
excluded from uniqueness and has no “Unique” entry. Pairwise gains against
vampire-hammer remain available in the report tables and CSVs.
The x-axis is labeled “Solved theorems”;
for averaged reports, it continues to represent the mean count across runs.
Tables and CSV files retain exact averaged counts and proof times in seconds.
Only accepted `true` results count. Errors remain unsolved; exclusions are
removed from the denominator. Zero times are displayed at one microsecond on logarithmic time axes; CSVs preserve recorded times.

`common-all/` uses the common eligible set across every tool. When Lean-SMT
is present, `common-without-lean-smt/` also reports the other tools on their
larger common eligible set. `pairwise-vs-baseline.csv` uses each pair's own
eligible set. `vs-baseline/` contains per-theorem outcomes/timings and separate
lists solved only by VampireHammer or only by its opponent. Its
`eligible_in_common_all` column permits filtering to the all-tool comparison.
Use `--baseline NAME` to change the reference tool or `--budget SECONDS` when
the result directory contains multiple search budgets.

To average independent repetitions, pass their result directories together:

```sh
python3 slurmrunner/plot-comparison.py \
  ~/comparison-full-no-bundled-results ~/comparison-full-no-bundled-results2 \
  --output artifacts/comparison-full-no-bundled-average
```

Each scope uses the intersection of completed, eligible theorems across every
selected tool and every run. Budgets and prover sets must match. The tables
report per-run solved counts, their arithmetic mean and sample standard
deviation, and how many theorems were solved in every run or in any run.
Unique counts and pairwise comparisons are computed within each run and then
averaged. The union of successes across runs is explicitly separate from the
mean solved count. Sample standard deviation describes variation between the
supplied runs; it is not a confidence interval.

Every scope also reports `theoretical-best`, the union of successes across its
selected tools. Each theorem counts once, at the fastest successful tool's
time. This models an ideal parallel portfolio with one worker per tool and no
overhead. The union is computed separately in each repetition, then averaged;
successes from different repetitions are not combined into one larger run.
The virtual portfolio appears as a black dotted cactus curve and a table row,
including its additional successes over the baseline. Its unique count is
blank because uniqueness applies to the individual tools. Their unique counts
continue to compare only the real tools.

The bold lin-log cactus curves show the mean number solved by each time
threshold, with faint individual-run curves. Equivalently, successful attempt
times are pooled and each contributes `1 / number_of_runs` to the x-axis.
Failures contribute no successful time. `cactus-data.csv` records these curves;
`problems-PROVER.csv` records statuses, measured times, success frequency, and
the mean time conditional on successful runs. `vs-baseline/` reports per-theorem
counts of both/only-baseline/only-opponent outcomes across repetitions. Numeric
means and standard deviations are also available in each scope's
`comparison.csv` and the top-level `summary.json`. Each input is audited
separately in `audit-run-N.json`. Missing or excluded cases are never silently
counted as failures; any cases dropped to match runs are reported.

`audit.json` records status/category counts, error messages and examples, kernel
check/timing violations, and discrepancies with the collector's JSON and TSV.
Theorem identities come from scheduled `--theorem` arguments, with task IDs as
a fallback; mismatches against reported theorem names are flagged. Reports
with such discrepancies are provisional. The script exits nonzero when audit
issues exist and refuses to plot invalid successful records. It checks
completeness against the collected files, not the original suite catalog, and
cannot recheck Lean proofs or reconstruct missing source journals.

The plot exports omit titles and subtitles; dataset context remains in the
report. Cactus tables identify the curves to the right of the axes. After each cactus plot,
`scatter-vs-baseline.*` compares every other real tool against VampireHammer
(on the x-axis with the default baseline). Each point is one theorem solved
by at least one of the two tools in at least one repetition. Cases neither tool
ever solved are excluded. Each measured coordinate is the arithmetic mean
across successful runs only. A tool with no success is shown in a shaded
“unsolved” band beyond the budget; that coordinate is categorical and is not
saved as a measured time. Deterministic jitter is added only along the
unsolved coordinate to reveal overlapping points; measured coordinates and
exported mean times are unchanged. Triangles distinguish mixed success across runs.
The axes use equal logarithmic scales and the dashed line shows equal time;
points above it favor the baseline. `scatter-data.csv` exports the theorem,
paired mean times (blank for no successes), success counts, and repetition
count. Dense points are rasterized at 300 dpi inside
PDF/SVG exports; labels and axes remain vector graphics.

Small-caps plot labels use Latin Modern Roman Caps (`LMRomanCaps10`) when
available; install the Latin Modern fonts to reproduce the publication fonts.
“Theoretical best” remains in the normal serif font.

LNCS exports have an exact width of 122 mm, matching `llncs.cls`’s 12.2 cm
text width. Base labels, ticks, and legends use the same physical size as the
9 TeX pt caption font (with the 72.27/72 point-unit conversion accounted for).
Cactus figures are 60 mm high with the plot on the left and a table on the right; combined scatter plots are 110 mm high.
Include the PDF with `\includegraphics[width=\textwidth]{...}`; additional
scaling changes the effective font size. Normal text uses Latin Modern Roman 9,
with Latin Modern Roman Caps for tool names. Censored scatter positions use
log-space jitter of ±0.4 decades; regular points use alpha 0.08 and
mixed-success triangles use alpha 0.25. Actual mean times are unchanged.

Scatter plots overlay all real opponents on one axes, with color identifying
the other tool. The x-axis uses the label `vampire-hammer`; mixed-success
triangles and the jittered unsolved bands retain the same meaning. Point order
is deterministically shuffled to avoid consistently drawing one tool on top.

The compact cactus table doubles as the legend using colored line samples.
Its columns are solver, mean solved count, and mean pairwise unique count
against vampire-hammer (solved by the row tool and unsolved by vampire-hammer
in the same repetition). Counts are rounded to integers for the figure only.
The table uses LLNCS `\scriptsize`, 7 TeX pt, while plot labels remain at
caption size (9 TeX pt). Theoretical best remains in normal serif type.

Figures use the abbreviations VH (Vampire-Hammer), LD (Lean-auto duper),
DU (Duper), AE (Aesop), SA (SimpAll), GR (Grind), LS (Lean-SMT), and TB
(Theoretical best), defined in the captions. All main cactus curves and their
table line samples use the same 1.2 pt thickness; individual-run traces remain
faint and use the same thin width for every solver.
