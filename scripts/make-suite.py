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
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

PROJECT = Path(__file__).resolve().parents[1]
TOOL_PROJECT = PROJECT
LAKE = ["lake", "-R"]

_spec = importlib.util.spec_from_file_location("benchmark_mathlib", PROJECT / "scripts/benchmark-mathlib.py")
benchmark = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(benchmark)

_isolated_spec = importlib.util.spec_from_file_location('isolated_backends', TOOL_PROJECT / 'scripts/hammer_bench/backends.py')
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


def native_lean_args(imports=None, lake=None, env=None, required_libraries=()):
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
            # Lean plugins initialize in dependency order, after external C
            # dynlibs. Loading an adapter as a plain dynlib can precede its
            # solver dependencies and leave unresolved native symbols.
            loaded = {Path(plugin['path']).resolve() for plugin in setup.get('plugins', [])}
            for library in required_libraries:
                result = subprocess.run(
                    [*lake, '--no-build', '--json', 'query', f'{library}:shared'],
                    cwd=PROJECT, capture_output=True, text=True, env=env)
                if result.returncode:
                    raise ValueError(f'native library {library} is not built; rebuild the backend')
                path = Path(json.loads(result.stdout)).resolve()
                if path not in loaded:
                    raise ValueError(f'native library {library} is not loaded as a Lean plugin; regenerate the prover '
                                     'project with prepare-provers.py and rebuild it')
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
    """The C parser library from the selected prover's dependency graph."""
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
        if any(row.get("catalog_strategy") != "lean-auto-style" for row in rows):
            raise ValueError('catalog must contain lean-auto-style testcases; omit --catalog to regenerate')
        return rows
    source = output / "Catalog.lean"
    catalog = output / "catalog.jsonl"
    source.write_text(benchmark.IMPORTS + "set_option maxHeartbeats 0\n" +
                      f"hammer_mathlib_proof_catalog {benchmark.lean_string(str(catalog))}\n")
    print("Extracting the theorem catalog from mathlib ...", flush=True)
    benchmark.run_lean(source, output / "catalog.log")
    return benchmark.read_rows(catalog)


def select(args, output):
    """Rows with name, module and stable testcase IDs."""
    if getattr(args, 'test_suite', None):
        root = args.test_suite.resolve()
        metadata = json.loads((root / 'test-suite.json').read_text())
        if metadata.get('version') != 1:
            raise ValueError('expected a version 1 test suite')
        if metadata['strategy'] != args.strategy:
            raise ValueError('test suite strategy differs from the requested strategy')
        return benchmark.read_rows(root / 'selection.jsonl'), metadata['eligible_theorems']
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
            f"input_files: {json.dumps(stem + '.thm', ensure_ascii=False)}\n"
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
    rundefinitions = [
        f'  <rundefinition name={quoteattr(prover + "-search" + str(seconds))}>\n'
        f'    <option name="--prover">{escape(getattr(args, "run_provers", {}).get(prover, prover))}</option>\n'
        f'    <option name="--config">{escape(str(config))}</option>\n'
        + (f'    <option name="--run-label">{escape(prover)}</option>\n'
           if prover in getattr(args, 'run_provers', {}) else '') +
        f'    <option name="--search-seconds">{seconds}</option>\n'
        '  </rundefinition>'
        for prover, config in args.backend_configs.items()
        for seconds in getattr(args, 'run_budgets', {}).get(prover, args.search_seconds)]
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
    command = (f' command={quoteattr(str(TOOL_PROJECT / "scripts/run-hammer-task"))}'
               if getattr(args, 'compact', False) else '')
    return f"""<?xml version="1.0"?>
<!DOCTYPE benchmark PUBLIC "+//IDN sosy-lab.org//DTD BenchExec benchmark 2.3//EN" "https://www.sosy-lab.org/benchexec/benchmark-2.3.dtd">
<!-- Generated by scripts/make-suite.py; see scripts/README.md.
     Select shards with BenchExec's task-set option, e.g. -t shard-0000. -->
<benchmark tool="hammer_bench.tool_info"{command}
           displayName="Tactic comparison on mathlib" {limits}>
{os.linesep.join(options)}
  <propertyfile>proves.prp</propertyfile>
  <resultfiles>hammer-artifacts/**</resultfiles>
{os.linesep.join(rundefinitions)}

{os.linesep.join(tasks)}
  <columns>
    <column title="outcome">outcome</column>
    <column title="route">route</column>
    <column title="prover">prover</column>
    <column title="hammer s">hammer-seconds</column>
    <column title="vampire s">vampire-seconds</column>
  </columns>
</benchmark>
"""


def apply_definition(parser, args):
    """Read our BenchExec-style experiment input, resolving paths beside the XML."""
    if not args.definition:
        return
    path = args.definition.resolve()
    try:
        root = ET.parse(path).getroot()
        if root.tag != 'benchmark':
            raise ValueError('definition root must be <benchmark>')
        allowed = {'success-seconds', 'timelimit',
                   'walltimelimit', 'hardtimelimit', 'memlimit', 'cpu-cores'}
        def options(element, allowed_names):
            values = []
            for child in element:
                if child.tag != 'option' or child.get('name') not in allowed_names:
                    raise ValueError(f'unsupported definition element/option: {child.tag} {child.get("name")}')
                name = child.get('name')
                values.append(name)
                values.append((child.text or '').strip())
            return values
        tokens = []
        for key, value in root.attrib.items():
            if key not in allowed:
                raise ValueError(f'unsupported benchmark attribute: {key}')
            tokens.extend(['--' + key, value])
        tasks = root.findall('tasks')
        if len(tasks) > 1:
            raise ValueError('definition accepts one <tasks> selection')
        if tasks:
            tokens += options(tasks[0], {'--test-suite', '--catalog', '--names', '--count', '--seed'})
        for index, token in enumerate(tokens[:-1]):
            if token in {'--test-suite', '--catalog', '--names'}:
                tokens[index + 1] = str((path.parent / tokens[index + 1]).resolve())
        # Explicit CLI options override XML selection and process settings.
        vars(args).update(vars(parser.parse_args(tokens + sys.argv[1:])))
        options_list = [child for child in root if child.tag == 'option']
        holder = ET.Element('shared')
        holder.extend(options_list)
        shared = options(holder, {'--case-option', '--search-seconds'})
        runs = []
        for rd in root.findall('rundefinition'):
            name = rd.get('name', '')
            if name == 'suite' or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*', name) or any(r['name'] == name for r in runs):
                raise ValueError(f'invalid or duplicate rundefinition name: {name!r}')
            rp = argparse.ArgumentParser(add_help=False)
            rp.add_argument('--prover', required=True)
            rp.add_argument('--case-option', action='append', default=[])
            rp.add_argument('--search-seconds', type=int, action='append')
            run = vars(rp.parse_args(shared + options(rd, {'--prover', '--case-option', '--search-seconds'})))
            run['name'] = name
            if run['search_seconds'] and any(s <= 0 for s in run['search_seconds']):
                raise ValueError('run search budgets must be positive')
            benchmark.case_options_prelude(run['case_option'])
            runs.append(run)
        if any(child.tag not in {'tasks', 'option', 'rundefinition'} for child in root):
            raise ValueError('unsupported definition element')
        if not runs and not args.tasks_only:
            raise ValueError('an experiment definition needs at least one <rundefinition>')
        args.run_definitions = runs
    except (ValueError, ET.ParseError, OSError) as error:
        parser.error(str(error))


def write_selection(args, output, rows, eligible):
    (output / 'selection.jsonl').write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows))
    manifest = json.loads((PROJECT / 'lake-manifest.json').read_text())
    mathlib_commit = next((p.get('rev') for p in manifest['packages'] if p['name'] == 'mathlib'), None)
    benchmark.write_json(output / 'test-suite.json', {
        'version': 1, 'strategy': args.strategy, 'count': len(rows),
        'eligible_theorems': eligible, 'seed': args.seed, 'mathlib_commit': mathlib_commit})


def main():
    global PROJECT, LAKE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backends', type=Path, help='registry from prepare-provers.py; each prover uses its own Lake project')
    parser.add_argument('--definition', type=Path, help='BenchExec-style experiment XML with tasks and named rundefinitions')
    parser.add_argument('--tasks-only', action='store_true', help='generate a reusable testcase selection without prover projects')
    parser.add_argument('--test-suite', type=Path, help='reuse a selection generated with --tasks-only')
    parser.add_argument('--prover', action='append', help='select a backend from --backends (repeatable; default all)')
    parser.add_argument("--project", type=Path, default=PROJECT,
                        help="built benchmark checkout (default: this script's repository)")
    parser.set_defaults(strategy='lean-auto-style', comparison=True)
    parser.add_argument('--format', choices=('benchexec', 'slurm'), default='benchexec',
                        help='benchexec (default): standard XML and per-theorem task files; slurm: compact JSONL task catalog')
    parser.add_argument("--success-seconds", type=float, default=10.0,
                        help="proof-success cutoff including premise extraction and kernel checking (default: 10 seconds)")
    source = parser.add_argument_group("theorem selection (default: every public mathlib theorem)")
    source.add_argument("--catalog", type=Path, help="existing catalog.jsonl; otherwise extracted from the current build")
    source.add_argument("--count", type=int, help="module-balanced seeded sample of this size (as benchmark-mathlib.py)")
    source.add_argument("--seed", default="mathlib-v4.34.1")
    source.add_argument("--names", type=Path, help="newline-separated theorem names")
    run = parser.add_argument_group("prover settings")
    run.add_argument("--vampire", help="Vampire executable (default: dependency .lake/vampire/build/vampire)")
    run.add_argument("--search-seconds", type=int, action="append", help="search budget in seconds; repeat for several rundefinitions (default: 10)")
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
    apply_definition(parser, args)
    args.compact = args.format == 'slurm'
    if not args.tasks_only and not args.backends:
        parser.error('--backends is required unless --tasks-only is used')
    if args.test_suite and any((args.catalog, args.names, args.count)):
        parser.error('--test-suite cannot be combined with other selection options')
    args.search_seconds = args.search_seconds or [10]
    if args.count is not None and args.count <= 0 or args.shard_size <= 0:
        parser.error('--count and --shard-size must be positive')
    if args.success_seconds is not None and args.success_seconds <= 0:
        parser.error('--success-seconds must be positive')
    if any(seconds <= 0 for seconds in args.search_seconds):
        parser.error('--search-seconds must be positive')
    args.case_option += [f'{name}=0' for name in benchmark.COMPARISON_HEARTBEAT_OPTIONS]
    PROJECT = args.project.expanduser().resolve()
    benchmark.PROJECT = PROJECT
    try:
        if args.tasks_only:
            output = args.output.resolve()
            if output.exists():
                raise ValueError(f'output already exists: {output}')
            output.mkdir(parents=True)
            rows, eligible = select(args, output)
            if not rows:
                raise ValueError('the theorem selection is empty')
            write_selection(args, output, rows, eligible)
            print(f'Wrote {len(rows)} reusable testcases to {output}')
            return 0
        return isolated.make_suite(args, globals())
    except ValueError as error:
        parser.error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
