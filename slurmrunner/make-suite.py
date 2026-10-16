#!/usr/bin/env python3
"""Generate a BenchExec suite that runs the hammer on mathlib theorems, one task each.

Run on the machine (or shared filesystem) where the suite will execute, after
`lake build VampireHammerBenchmark`: the suite records
absolute paths to that build, its Lean environment and the Vampire binary.
"""

import argparse
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import subprocess
import sys
import tempfile
import tarfile
from xml.sax.saxutils import escape, quoteattr

PROJECT = Path(__file__).resolve().parents[1]
TOOL_PROJECT = PROJECT
LAKE = ["lake", "-R"]
PROVERS = ("vampire-hammer", "duper", "aesop", "simp_all", "grind")
COMPARISON_IMPORTS = "import Mathlib\nimport VampireHammerBenchmark.Comparison\n"

_spec = importlib.util.spec_from_file_location("benchmark_mathlib", PROJECT / "scripts/benchmark-mathlib.py")
benchmark = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(benchmark)


def output_of(command, **kwargs):
    return subprocess.check_output(command, cwd=PROJECT, text=True, **kwargs).strip()


def lean_environment():
    """The variables `lake env` adds or changes, so tasks can run `lean` directly."""
    def parse(raw):
        return dict(item.split("=", 1) for item in raw.split("\0") if "=" in item)
    base = parse(output_of(["env", "-0"]))
    lake = parse(output_of([*LAKE, "env", "env", "-0"], stderr=subprocess.DEVNULL))
    # PATH stays the node's own; LEAN_RECURSION_COUNT is lake's internal guard.
    changed = {key: value for key, value in lake.items()
               if base.get(key) != value and key not in {"PATH", "LEAN_RECURSION_COUNT"}}
    # elan's shim resolves the toolchain from the working directory; tasks run
    # elsewhere, so they use the toolchain's binary directly.
    prefix = output_of([*LAKE, "env", "lean", "--print-prefix"], stderr=subprocess.DEVNULL)
    return str(Path(prefix) / "bin" / "lean"), changed


def native_lean_args(imports=None, lake=None):
    """`lean` flags that load the precompiled shared libraries, as `lake lean` does.

    Without them, `lean` runs the hammer's and lean-auto's metaprograms in its
    interpreter, several times slower. Lake decides which libraries a file with
    the benchmark's imports needs; this reads that decision from its verbose
    `module setup` trace instead of re-deriving it."""
    imports = benchmark.IMPORTS if imports is None else imports
    lake = LAKE if lake is None else lake
    with tempfile.TemporaryDirectory() as directory:
        probe = Path(directory) / "Probe.lean"
        probe.write_text(imports)
        result = subprocess.run([*lake, "-v", "lean", str(probe)], cwd=PROJECT,
                                capture_output=True, text=True)
    if result.returncode:
        sys.exit("could not load the benchmark imports/native libraries:\n" +
                 (result.stdout + result.stderr)[-2000:])
    marker = "module setup: "
    for line in (result.stdout + result.stderr).splitlines():
        if marker in line:
            setup = json.loads(line.split(marker, 1)[1])
            # A precompiled library with several roots is not a plugin, so
            # Lean loads it at startup, before the plugins it needs (lean-auto).
            misplaced = [library for library in setup.get("dynlibs", [])
                         if Path(library).name.startswith("libvampire__hammer_")]
            if misplaced:
                sys.exit("Lake loads the hammer's native code as a dynlib, before lean-auto: "
                         f"{misplaced}. Keep each hammer `lean_lib` to a single root.")
            # Plugins can depend on the dynlibs (the hammer links against the
            # TPTP parser), so the dynlibs are loaded first.
            return ([f"--load-dynlib={library}" for library in setup.get("dynlibs", [])] +
                    [f"--plugin={plugin['path']}" for plugin in setup.get("plugins", [])])
    sys.exit("could not read Lake's module setup for the benchmark imports:\n" +
             (result.stdout + result.stderr)[-2000:])


def upstream_config(project, common, prover='lean-auto-duper'):
    """Record a separate Lean runtime; never mix the two Auto module trees."""
    global PROJECT, LAKE
    old_project, old_lake = PROJECT, LAKE
    PROJECT, LAKE = project.expanduser().resolve(), ["lake"]
    try:
        manifest = json.loads((PROJECT / 'lake-manifest.json').read_text())
        packages = {p['name']: p for p in manifest['packages']}
        auto = packages['auto']
        preparation = json.loads((PROJECT / 'upstream-benchmark.json').read_text())
        tool_file = 'prepare-lean-smt.py' if prover == 'lean-smt' else 'prepare-lean-auto.py'
        harness = old_project / 'VampireHammerBenchmark/Mathlib.lean'
        if preparation['harness_sha256'] != hashlib.sha256(harness.read_bytes()).hexdigest():
            sys.exit(f'Upstream harness is stale; regenerate with {tool_file} and rebuild')
        spec = importlib.util.spec_from_file_location('prepare_upstream', TOOL_PROJECT / 'slurmrunner' / tool_file)
        preparation_tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(preparation_tool)
        if auto.get('url', '').removesuffix('.git') != 'https://github.com/leanprover-community/lean-auto' or auto.get('rev') != preparation_tool.AUTO_REV:
            sys.exit('The lean-auto baseline must use the pinned normal upstream lean-auto')
        if prover == 'lean-smt':
            for name, url, revision in (
                ('smt', 'https://github.com/ufmg-smite/lean-smt', preparation_tool.SMT_REV),
                ('cvc5', 'https://github.com/abdoo8080/lean-cvc5', preparation_tool.CVC5_REV)):
                package = packages.get(name, {})
                dependency = PROJECT / '.lake/packages' / name
                if (package.get('url', '').removesuffix('.git') != url or package.get('rev') != revision
                        or output_of(['git', '-C', str(dependency), 'rev-parse', 'HEAD']) != revision
                        or output_of(['git', '-C', str(dependency), 'status', '--porcelain'])):
                    sys.exit(f'Lean-SMT requires a clean, pinned {name} dependency')
        if (PROJECT / 'VampireHammerBenchmark/Mathlib.lean').read_text() != preparation_tool.harness_source(harness.read_text()) or (PROJECT / 'VampireHammerBenchmark/Comparison.lean').read_text() != preparation_tool.COMPARISON:
            sys.exit('Regenerate the upstream benchmark: its harness or adapter has been changed')
        auto_dir = PROJECT / '.lake/packages/auto'
        if output_of(['git', '-C', str(auto_dir), 'rev-parse', 'HEAD']) != auto['rev'] or output_of(['git', '-C', str(auto_dir), 'status', '--porcelain']):
            sys.exit('Upstream lean-auto checkout must be clean and match its recorded revision')
        mathlib = output_of(['git', '-C', str(PROJECT / '.lake/packages/mathlib'), 'rev-parse', 'HEAD'])
        if mathlib != common['mathlib_commit']:
            sys.exit('Both comparison projects must use the same mathlib commit')
        for name in ('Duper', 'aesop'):
            if prover == 'lean-smt' and name == 'Duper':
                continue
            if packages[name].get('rev') != common['dependency_revisions'].get(name):
                sys.exit(f'Both comparison projects must use the same {name} revision')
        probe = subprocess.run(['lake', 'build', '--no-build', 'VampireHammerBenchmark.Comparison'],
                               cwd=PROJECT, capture_output=True, text=True)
        if probe.returncode:
            sys.exit('Build the isolated upstream project first:\n' + probe.stdout + probe.stderr)
        lean, env = lean_environment()
        if output_of([lean, '--version']) != common['lean_version']:
            sys.exit('Both comparison projects must use the same Lean version')
        config = dict(common)
        config.update(project=str(PROJECT), lean=lean, lean_env=env,
                      lean_args=native_lean_args(imports=COMPARISON_IMPORTS),
                      dependency_revisions={name: p.get('rev') for name, p in packages.items()},
                      provers=[prover], diagnostic_provers=[])
        config.pop('vampire', None)
        config.pop('vampire_sha256', None)
        config.pop('tptp_parser_dynlib', None)
        config['upstream_source_hashes'] = {
            name: hashlib.sha256((PROJECT / name).read_bytes()).hexdigest()
            for name in ('lakefile.lean', 'lake-manifest.json', 'lean-toolchain',
                         'VampireHammerBenchmark/Mathlib.lean', 'VampireHammerBenchmark/Comparison.lean')}
        return config
    finally:
        PROJECT, LAKE = old_project, old_lake


def parser_dynlib():
    """The C parser library: vendored copy if present, else the sibling checkout."""
    root = benchmark.package_root('tptp_parser', PROJECT)
    library = root / '.lake/build/lib/libtptp_cparser.so'
    if library.exists():
        return library.resolve()
    sys.exit('libtptp_cparser.so not found; build the Vampire-Hammer dependency first')


def check_built():
    targets = ["VampireHammer", "VampireHammerBenchmark.Mathlib",
               "VampireHammerBenchmark.Comparison"]
    probe = subprocess.run([*LAKE, "build", "--no-build", *targets],
                           cwd=PROJECT, capture_output=True, text=True)
    if probe.returncode:
        sys.exit("The hammer is not built (or is out of date). Run first:\n"
                 "  " + " ".join([*LAKE, "build", *targets]) + "\n" +
                 (probe.stdout + probe.stderr)[-2000:])


def hammer_revision():
    metadata = benchmark.hammer_project(PROJECT) / '.benchmark-source.json'
    if metadata.exists():
        recorded = json.loads(metadata.read_text())
        return recorded['revision'][:12] + ('-dirty' if recorded.get('dirty') else '') + '-snapshot'
    revision = output_of(["git", "-C", str(benchmark.hammer_project(PROJECT)), "rev-parse", "--short=12", "HEAD"])
    dirty = output_of(["git", "-C", str(benchmark.hammer_project(PROJECT)), "status", "--porcelain", "--untracked-files=no", "."])
    return revision + ("-dirty" if dirty else "")


def snapshot(output, compact=False):
    if compact:
        hashes = {}
        with tarfile.open(output / 'source-snapshot.tar.gz', 'w:gz', compresslevel=3, dereference=True) as archive:
            sources = list(benchmark.source_files())
            sources += [(f'project/{relative}', PROJECT / relative) for relative in (
                'VampireHammerBenchmark/Mathlib.lean', 'VampireHammerBenchmark/Comparison.lean',
                'VampireHammerBenchmark.lean', 'lakefile.lean', 'lake-manifest.json', 'lean-toolchain')]
            for key, path in sources:
                if path.is_file():
                    hashes[key] = hashlib.sha256(path.read_bytes()).hexdigest()
                    archive.add(path, arcname=key, recursive=False)
        benchmark.write_json(output / 'source-hashes.json', hashes)
        return hashes
    hashes = benchmark.snapshot(output)
    # The helper excludes Benchmark/; record the harness and dependency pins too.
    for relative in ("VampireHammerBenchmark/Mathlib.lean", "VampireHammerBenchmark/Comparison.lean", "lakefile.lean",
                     "VampireHammerBenchmark.lean", "lake-manifest.json", "lean-toolchain"):
        path = PROJECT / relative
        if path.is_file():
            key = f"project/{relative}"
            destination = output / "source-snapshot" / key
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
            hashes[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    benchmark.write_json(output / "source-hashes.json", hashes)
    return hashes


def catalog_rows(args, output):
    if args.catalog:
        rows = benchmark.read_rows(args.catalog)
        if args.strategy == "lean-auto-style" and any(row.get("catalog_strategy") != "lean-auto-style" for row in rows):
            sys.exit("--strategy lean-auto-style requires a catalog generated with that strategy; omit --catalog")
        return rows
    source = output / "Catalog.lean"
    catalog = output / "catalog.jsonl"
    command = "hammer_mathlib_proof_catalog" if args.strategy == "lean-auto-style" else "hammer_mathlib_catalog"
    source.write_text(benchmark.IMPORTS + "set_option maxHeartbeats 0\n" +
                      f"{command} {benchmark.lean_string(str(catalog))}\n")
    print("Extracting the theorem catalog from mathlib ...", flush=True)
    benchmark.run_lean(source, output / "catalog.log")
    return benchmark.read_rows(catalog)


def select(args, output):
    """Rows with name, module and a stable id (kept from --corpus)."""
    if args.corpus:
        return benchmark.select_from_corpus(args.corpus, args.names, args.count or sys.maxsize), None
    rows = catalog_rows(args, output)
    eligible = len(rows)
    if args.names:
        requested = [line.strip() for line in args.names.read_text().splitlines() if line.strip()]
        by_name = {row["name"]: row for row in rows}
        missing = [name for name in requested if name not in by_name]
        if missing:
            sys.exit(f"{len(missing)} names are not public mathlib theorems, e.g. {missing[:5]}")
        rows = [by_name[name] for name in dict.fromkeys(requested)]
    elif args.count:
        rows = benchmark.sample(rows, args.count, args.seed)
    else:
        rows = sorted(rows, key=lambda row: (row["module"], row["name"]))
    for index, row in enumerate(rows):
        row["id"] = index
    return rows, eligible


def task_stem(row):
    safe = re.sub(r"[^A-Za-z0-9._'-]", "_", row["name"])[:120]
    return f'{row["id"]:06d}-{safe}'


def write_tasks(rows, output, shard_size, compact=False):
    shards = {}
    index = []
    for row in rows:
        shard = row["id"] // shard_size
        stem = task_stem(row)
        if compact:
            shards[shard] = shards.get(shard, 0) + 1
            index.append({'id': row['id'], 'name': row['name'], 'module': row['module'],
                          'shard': shard, 'task': f'theorems.jsonl#{stem}'})
            continue
        directory = output / "tasks" / f"{shard:04d}"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{stem}.thm").write_text(row["name"] + "\n" + row["module"] + "\n")
        (directory / f"{stem}.yml").write_text(
            "format_version: '2.0'\n"
            f"input_files: '{stem}.thm'\n"
            "properties:\n"
            "  - property_file: ../../proves.prp\n"
            "    expected_verdict: true\n"
            "options:\n"
            f"  theorem: {json.dumps(row['name'], ensure_ascii=False)}\n"
            f"  module: {json.dumps(row['module'])}\n")
        shards[shard] = shards.get(shard, 0) + 1
        index.append({"id": row["id"], "name": row["name"], "module": row["module"],
                      "task": f"tasks/{shard:04d}/{stem}.yml"})
    (output / "theorems.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in index))
    return shards


def benchmark_xml(args, config_path, shards):
    options = [f'  <option name="--config">{escape(str(config_path))}</option>']
    if args.keep_artifacts:
        options.append('  <option name="--keep-artifacts"/>')
    for pair in args.case_option:
        options.append(f'  <option name="--case-option">{escape(pair)}</option>')
    comparison = getattr(args, 'comparison', False)
    upstream = getattr(args, 'lean_auto_project', None)
    smt_project = getattr(args, 'lean_smt_project', None)
    provers = (("vampire-hammer", "lean-auto-duper", *PROVERS[1:]) if upstream else PROVERS) if comparison else ("vampire-hammer",)
    if smt_project:
        provers = (*provers, 'lean-smt')
    if upstream or smt_project:
        options = [line for line in options if 'name="--config"' not in line]
    def config_for(prover):
        return config_path.parent / {'lean-auto-duper': 'lean-auto-config.json',
                                     'lean-smt': 'lean-smt-config.json'}.get(prover, config_path.name)
    rundefinitions = [
        f'  <rundefinition name="{prover + "-" if comparison else ""}search{seconds}">\n'
        + (f'    <option name="--prover">{prover}</option>\n' if comparison else '') +
        (f'    <option name="--config">{escape(str(config_for(prover)))}</option>\n' if upstream or smt_project else '') +
        f'    <option name="--search-seconds">{seconds}</option>\n'
        '  </rundefinition>'
        for prover in provers for seconds in args.search_seconds]
    tasks = [
        f'  <tasks name="shard-{shard:04d}">\n'
        + (f'    <include>theorems.jsonl#{shard * args.shard_size}:{(shard + 1) * args.shard_size}</include>\n'
           if getattr(args, 'compact', False) else f'    <include>tasks/{shard:04d}/*.yml</include>\n') +
        '  </tasks>'
        for shard in sorted(shards)]
    limits = f'timelimit={quoteattr(args.timelimit)} memlimit={quoteattr(args.memlimit)} cpuCores="{args.cpu_cores}"'
    if args.hardtimelimit:
        limits += f" hardtimelimit={quoteattr(args.hardtimelimit)}"
    if args.walltimelimit:
        limits += f" walltimelimit={quoteattr(args.walltimelimit)}"
    return f"""<?xml version="1.0"?>
<!DOCTYPE benchmark PUBLIC "+//IDN sosy-lab.org//DTD BenchExec benchmark 2.3//EN" "https://www.sosy-lab.org/benchexec/benchmark-2.3.dtd">
<!-- Generated by slurmrunner/make-suite.py; see slurmrunner/README.md.
     Select shards with BenchExec's task-set option, e.g. -t shard-0000.
     `command` is for slurmbench.py; BenchExec uses the tool-info module. -->
<benchmark tool="hammer_bench.tool_info" command={quoteattr(str(TOOL_PROJECT / "slurmrunner/run-hammer-task"))}
           displayName="{'Tactic comparison on mathlib' if comparison else 'Vampire hammer on mathlib'}" {limits}>
{os.linesep.join(options)}
  <propertyfile>proves.prp</propertyfile>
  <resultfiles>hammer-artifacts/**</resultfiles>
  <columns>
    <column title="outcome">outcome</column>
    <column title="route">route</column>
    <column title="prover">prover</column>
    <column title="hammer s">hammer-seconds</column>
    <column title="vampire s">vampire-seconds</column>
  </columns>

{os.linesep.join(rundefinitions)}

{os.linesep.join(tasks)}
</benchmark>
"""


def main():
    global PROJECT, LAKE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=PROJECT,
                        help="built benchmark checkout (default: this script's repository)")
    parser.add_argument("--lean-auto-project", type=Path,
                        help="built isolated upstream project from prepare-lean-auto.py; adds lean-auto-duper to --comparison")
    parser.add_argument('--lean-smt-project', type=Path,
                        help='built isolated project from prepare-lean-smt.py; adds the kernel-checked cvc5 smt tactic')
    parser.add_argument("--comparison", action="store_true",
                        help="compare vampire-hammer, duper, aesop, simp_all and grind with common declaration-time premises and wall-clock budgets; disables heartbeats")
    parser.add_argument('--compact', action='store_true',
                        help='Slurm-only suite: one JSONL task catalog and archived source snapshot instead of per-theorem files')
    parser.add_argument("--strategy", choices=("hammer", "lean-auto-style"), default="hammer",
                        help="hammer: automatic premises; lean-auto-style: declaration-time source replay, proof-derived premises, no heartbeat limit and a 10s success cutoff")
    parser.add_argument("--success-seconds", type=float,
                        help="maximum measured proof time for a success; lean-auto-style includes premise extraction and kernel checking, excludes startup/source replay/audit; default: 10 for lean-auto-style, unlimited for hammer")
    source = parser.add_argument_group("theorem selection (default: every public mathlib theorem)")
    source.add_argument("--catalog", type=Path, help="existing catalog.jsonl; otherwise extracted from the current build")
    source.add_argument("--count", type=int, help="module-balanced seeded sample of this size (as benchmark-mathlib.py)")
    source.add_argument("--seed", default="mathlib-v4.29.0-hammer-20261007")
    source.add_argument("--names", type=Path, help="newline-separated theorem names")
    source.add_argument("--corpus", type=Path, help="reuse an extracted corpus' lemmas.jsonl and ids (e.g. artifacts/mathlib-corpus-20261007)")
    run = parser.add_argument_group("hammer settings")
    run.add_argument("--skip-vampire-build", action="store_true",
                     help="use an existing --vampire binary without building the native prover during Lake checks")
    run.add_argument("--vampire", help="Vampire executable (default: dependency .lake/vampire/build/vampire)")
    run.add_argument("--search-seconds", type=int, action="append", help="Vampire search budget; repeat for several rundefinitions (default: hammer 20, lean-auto-style 10)")
    run.add_argument("--vampire-memory-mib", type=int, default=512)
    run.add_argument("--case-option", action="append", default=[], metavar="NAME=VALUE",
                     help="extra 'set_option NAME VALUE in' for every case (repeatable)")
    run.add_argument("--keep-artifacts", action="store_true", help="retain exported problems and Vampire output as BenchExec result files")
    limits = parser.add_argument_group("BenchExec limits (whole Lean process, including `import Mathlib`)")
    limits.add_argument("--timelimit", default="600 s",
                        help="process wall-clock limit, including imports and source replay; packed module batches share seconds x batch size of wall time")
    limits.add_argument("--hardtimelimit", default=None)
    limits.add_argument("--walltimelimit", default="660 s")
    limits.add_argument("--memlimit", default="8 GB")
    limits.add_argument("--cpu-cores", type=int, default=1)
    parser.add_argument("--shard-size", type=int, default=1000, help="tasks per <tasks> set")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    LAKE = ["lake", "-R"]
    if args.skip_vampire_build:
        LAKE.append('-KbuildVampire=false')
    PROJECT = args.project.expanduser().resolve()
    benchmark.PROJECT = PROJECT
    benchmark.LAKE = LAKE
    if args.comparison:
        args.strategy = "lean-auto-style"
        if any(pair.partition('=')[0] in benchmark.COMPARISON_HEARTBEAT_OPTIONS
               and pair.partition('=')[2] != '0' for pair in args.case_option):
            parser.error('--comparison uses time limits; heartbeat limits must be 0')
    args.search_seconds = args.search_seconds or ([10] if args.strategy == "lean-auto-style" else [20])
    if args.strategy == "lean-auto-style":
        if args.corpus:
            parser.error("lean-auto-style extracts its own human-theorem catalog; use --catalog/--names instead of --corpus")
        args.case_option = ["maxHeartbeats=0", "vampire.suggestedLemmas=0",
                            "vampire.siblingLemmas=false", "vampire.omitUnsupportedLocalHypotheses=true",
                            "vampire.alignNestedDefinitions=true", *args.case_option]
        if args.comparison:
            args.case_option = [f'{name}=0' for name in benchmark.COMPARISON_HEARTBEAT_OPTIONS
                                if name != 'maxHeartbeats'] + args.case_option
            args.case_option.insert(1, 'maxSaturationIteration=1000000000')
        if args.success_seconds is None:
            args.success_seconds = 10.0
    if args.success_seconds is not None and args.success_seconds <= 0:
        parser.error("--success-seconds must be positive")
    if args.corpus and args.catalog:
        parser.error("--corpus and --catalog are alternatives")
    if args.count is not None and args.count <= 0 or args.shard_size <= 0:
        parser.error("--count and --shard-size must be positive")

    if args.lean_auto_project and not args.comparison:
        parser.error('--lean-auto-project requires --comparison')
    if args.lean_smt_project and not args.comparison:
        parser.error('--lean-smt-project requires --comparison')
    check_built()
    vampire = (Path(args.vampire).expanduser().resolve() if args.vampire else
               benchmark.vampire_executable(PROJECT))
    if not os.access(vampire, os.X_OK):
        parser.error(f"Vampire executable not found: {vampire}; build the repository or pass --vampire")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)

    lean, lean_env = lean_environment()
    config = {
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "project": str(PROJECT), "hammer_revision": hammer_revision(),
        "hammer_project": str(benchmark.hammer_project(PROJECT)),
        "hammer_source": benchmark.source_provenance(benchmark.hammer_project(PROJECT)),
        "benchmark_source": benchmark.source_provenance(PROJECT),
        "vampire_version": subprocess.run([str(vampire), '--version'], capture_output=True,
                                           text=True, check=True, timeout=10).stdout.strip(),
        "lean": lean, "lean_env": lean_env,
        "lean_args": native_lean_args(imports=COMPARISON_IMPORTS) if args.comparison else native_lean_args(),
        "lean_version": output_of([lean, "--version"]),
        "mathlib_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=PROJECT / ".lake/packages/mathlib", text=True).strip(),
        "tptp_parser_dynlib": str(parser_dynlib()),
        "vampire": str(vampire), "vampire_sha256": benchmark.binary_hash(vampire),
        "vampire_memory_mib": args.vampire_memory_mib, "search_seconds": args.search_seconds[0],
        "case_options": args.case_option,
        "evaluation_strategy": args.strategy, "success_seconds": args.success_seconds,
        "comparison": args.comparison, "provers": list(PROVERS) if args.comparison else ["vampire-hammer"],
        "diagnostic_provers": ["vampire-hammer"],
        "benchmark_imports": COMPARISON_IMPORTS if args.comparison else benchmark.IMPORTS,
        "proof_timeout_seconds": args.success_seconds if args.comparison else None,
        "methodology": {"premises": "proof-theorems" if args.strategy == "lean-auto-style" else "automatic",
                        "environment": "before-declaration-command" if args.strategy == "lean-auto-style" else "full-import-with-target-reuse-audit",
                        "catalog": "user-declared" if args.strategy == "lean-auto-style" else "public-theorems",
                        "same_command_filter": args.strategy == "lean-auto-style", "original_paper_dataset": False,
                        "success_time_includes": ["premise-extraction", "tactic", "kernel-check"] if args.strategy == "lean-auto-style" else ["harness"],
                        "source_replay": args.strategy == "lean-auto-style"},
    }
    if args.comparison:
        duration = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*([smh]?)\s*', args.timelimit)
        if not duration:
            parser.error('--comparison --timelimit must use seconds, minutes or hours (e.g. "180 s")')
        config['process_wall_seconds'] = float(duration[1]) * {'': 1, 's': 1, 'm': 60, 'h': 3600}[duration[2]]
        config['methodology'].update(heartbeat_limit=0, proof_budget='wall-clock',
                                     same_imports_for_all_provers=True,
                                     duper_iteration_limit=1000000000,
                                     aesop_limits={'maxRuleApplications': 0, 'maxRuleApplicationDepth': 0,
                                                   'maxNormIterations': 1000000000, 'maxSafePrefixRuleApplications': 0},
                                     baseline_tactics={'duper': 'intros; duper [*, premises]',
                                                      'aesop': 'intros; aesop (add unsafe premises)',
                                                      'simp_all': 'intros; simp_all [premises]',
                                                      'grind': 'intros; grind [premises]'})
        manifest = json.loads((PROJECT / 'lake-manifest.json').read_text()) if (PROJECT / 'lake-manifest.json').exists() else {}
        config['dependency_revisions'] = {p['name']: p.get('rev') for p in manifest.get('packages', [])}
    for project, prover, filename, snapshot_dir in (
            (args.lean_auto_project, 'lean-auto-duper', 'lean-auto-config.json', 'upstream-source-snapshot'),
            (args.lean_smt_project, 'lean-smt', 'lean-smt-config.json', 'lean-smt-source-snapshot')):
        if not project:
            continue
        upstream = upstream_config(project, config, prover) if prover == 'lean-smt' else upstream_config(project, config)
        if prover == 'lean-auto-duper':
            config['provers'].insert(1, prover)
        else:
            config['provers'].append(prover)
        config['methodology']['same_imports_for_all_provers'] = False
        config['methodology']['upstream_baseline_isolated'] = True
        config['methodology']['baseline_tactics'][prover] = (
            'intros; smt +mono -trust -native [*, premises]' if prover == 'lean-smt' else
            'intros; auto [*, premises] with upstream documented duperRaw adapter')
        key = 'lean_smt_baseline' if prover == 'lean-smt' else 'upstream_baseline'
        config[key] = {'project': upstream['project'], 'dependency_revisions': upstream['dependency_revisions']}
        benchmark.write_json(output / filename, upstream)
        for relative in upstream['upstream_source_hashes']:
            destination = output / snapshot_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(Path(upstream['project']) / relative, destination)
    rows, eligible = select(args, output)
    if not rows:
        parser.error("the theorem selection is empty")
    config.update({"selection": {"count": len(rows), "eligible_theorems": eligible,
                                 "modules": len({row["module"] for row in rows}),
                                 "corpus": str(args.corpus) if args.corpus else None,
                                 "names": str(args.names) if args.names else None,
                                 "sample_count": args.count, "seed": args.seed if args.count else None},
                   "benchexec": {"search_seconds": args.search_seconds, "timelimit": args.timelimit,
                                 "walltimelimit": args.walltimelimit, "memlimit": args.memlimit,
                                 "cpu_cores": args.cpu_cores}})
    config["source_hashes"] = snapshot(output, compact=True) if args.compact else snapshot(output)
    config_path = output / "suite-config.json"
    benchmark.write_json(config_path, config)

    (output / "proves.prp").write_text(
        "The hammer produces an independent, kernel-checked proof of the theorem's original statement.\n")
    shards = write_tasks(rows, output, args.shard_size, compact=args.compact)
    (output / "vampire-hammer.xml").write_text(benchmark_xml(args, config_path, shards))
    print(f"Wrote {len(rows)} tasks in {len(shards)} shards to {output}")
    print(f"  benchmark definition: {output / 'vampire-hammer.xml'}")
    task_argument = (f"--theorem {shlex.quote(rows[0]['name'])} --module {shlex.quote(rows[0]['module'])}"
                     if args.compact else str(output / 'tasks/0000' / (task_stem(rows[0]) + '.thm')))
    print(f"  try one task:  {TOOL_PROJECT / 'slurmrunner/run-hammer-task'} --config {config_path} {task_argument}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
