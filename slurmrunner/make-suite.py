#!/usr/bin/env python3
"""Generate a BenchExec/Slurm suite from built isolated prover projects.

Run on the shared filesystem where the suite will execute. The registry
from prepare-provers.py supplies each prover's build and Lean environment.
"""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from xml.sax.saxutils import escape, quoteattr

PROJECT = Path(__file__).resolve().parents[1]
TOOL_PROJECT = PROJECT
LAKE = ["lake", "-R"]

_spec = importlib.util.spec_from_file_location("benchmark_mathlib", PROJECT / "scripts/benchmark-mathlib.py")
benchmark = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(benchmark)

_isolated_spec = importlib.util.spec_from_file_location('isolated_backends', TOOL_PROJECT / 'slurmrunner/isolated.py')
isolated = importlib.util.module_from_spec(_isolated_spec)
_isolated_spec.loader.exec_module(isolated)


def duration_seconds(value):
    match = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*([smh]?)\s*', value)
    if not match or float(match[1]) <= 0:
        raise ValueError('time limit must be positive seconds, minutes or hours, e.g. "600 s"')
    return float(match[1]) * {'': 1, 's': 1, 'm': 60, 'h': 3600}[match[2]]


def output_of(command, **kwargs):
    return subprocess.check_output(command, cwd=PROJECT, text=True, **kwargs).strip()


def lean_environment(clean=False):
    """The variables `lake env` adds or changes, so tasks can run `lean` directly."""
    def parse(raw):
        return dict(item.split("=", 1) for item in raw.split("\0") if "=" in item)
    extra = {'env': isolated.clean_environment()} if clean else {}
    base = parse(output_of(["env", "-0"], **extra))
    lake = parse(output_of([*LAKE, "env", "env", "-0"], stderr=subprocess.DEVNULL, **extra))
    # PATH stays the node's own; LEAN_RECURSION_COUNT is lake's internal guard.
    changed = {key: value for key, value in lake.items()
               if base.get(key) != value and key not in {"PATH", "LEAN_RECURSION_COUNT"}}
    # elan's shim resolves the toolchain from the working directory; tasks run
    # elsewhere, so they use the toolchain's binary directly.
    prefix = output_of([*LAKE, "env", "lean", "--print-prefix"], stderr=subprocess.DEVNULL, **extra)
    return str(Path(prefix) / "bin" / "lean"), changed


def native_lean_args(imports=None, lake=None, env=None):
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
                                capture_output=True, text=True, env=env)
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


def parser_dynlib():
    """The C parser library: vendored copy if present, else the sibling checkout."""
    root = benchmark.package_root('tptp_parser', PROJECT)
    library = root / '.lake/build/lib/libtptp_cparser.so'
    if library.exists():
        return library.resolve()
    sys.exit('libtptp_cparser.so not found; build the Vampire-Hammer dependency first')


def hammer_revision():
    metadata = benchmark.hammer_project(PROJECT) / '.benchmark-source.json'
    if metadata.exists():
        recorded = json.loads(metadata.read_text())
        return recorded['revision'][:12] + ('-dirty' if recorded.get('dirty') else '') + '-snapshot'
    revision = output_of(["git", "-C", str(benchmark.hammer_project(PROJECT)), "rev-parse", "--short=12", "HEAD"])
    dirty = output_of(["git", "-C", str(benchmark.hammer_project(PROJECT)), "status", "--porcelain", "--untracked-files=no", "."])
    return revision + ("-dirty" if dirty else "")


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
    options = []
    if args.keep_artifacts:
        options.append('  <option name="--keep-artifacts"/>')
    for pair in args.case_option:
        options.append(f'  <option name="--case-option">{escape(pair)}</option>')
    rundefinitions = [
        f'  <rundefinition name="{escape(prover)}-search{seconds}">\n'
        f'    <option name="--prover">{escape(prover)}</option>\n'
        f'    <option name="--config">{escape(str(config))}</option>\n'
        f'    <option name="--search-seconds">{seconds}</option>\n'
        '  </rundefinition>'
        for prover, config in args.backend_configs.items() for seconds in args.search_seconds]
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
           displayName="Tactic comparison on mathlib" {limits}>
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
    parser.add_argument('--backends', type=Path, required=True, help='registry from prepare-provers.py; each prover uses its own Lake project')
    parser.add_argument('--prover', action='append', help='select a backend from --backends (repeatable; default all)')
    parser.add_argument("--project", type=Path, default=PROJECT,
                        help="built benchmark checkout (default: this script's repository)")
    parser.add_argument("--comparison", action="store_true",
                        help="compare selected backends with declaration-time premises and wall-clock budgets")
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
    if args.comparison:
        args.strategy = 'lean-auto-style'
    args.search_seconds = args.search_seconds or [10]
    if args.count is not None and args.count <= 0 or args.shard_size <= 0:
        parser.error('--count and --shard-size must be positive')
    if args.corpus and args.catalog:
        parser.error('--corpus and --catalog are alternatives')
    if args.success_seconds is None and args.strategy == 'lean-auto-style':
        args.success_seconds = 10.0
    if args.success_seconds is not None and args.success_seconds <= 0:
        parser.error('--success-seconds must be positive')
    if any(seconds <= 0 for seconds in args.search_seconds):
        parser.error('--search-seconds must be positive')
    args.case_option += [f'{name}=0' for name in benchmark.COMPARISON_HEARTBEAT_OPTIONS]
    PROJECT = args.project.expanduser().resolve()
    benchmark.PROJECT = PROJECT
    try:
        return isolated.make_suite(args, globals())
    except ValueError as error:
        parser.error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
