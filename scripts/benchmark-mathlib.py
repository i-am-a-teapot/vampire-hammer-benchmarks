#!/usr/bin/env python3
"""Extract actual mathlib statements and benchmark the current hammer without changing it."""

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import threading
import time

PROJECT = Path(__file__).resolve().parents[1]
IMPORTS = "import Mathlib\nimport VampireHammerBenchmark.Mathlib\n"
LAKE = ["lake", "-R"]


def lean_command(source, *options):
    """Elaborate `source` as the editor does. `lake lean` loads the precompiled
    shared libraries of the hammer and lean-auto; `lake env lean` would run
    their code in Lean's interpreter instead, several times slower, and
    misrepresent the phase times. The source precedes Lean's own options."""
    return [*LAKE, "lean", str(source), "--", "-j", "1", *options]


def package_root(name, project=None):
    """Resolve Git and local Lake dependencies from the resolved manifest."""
    project = Path(project or PROJECT).resolve()
    manifest = project / 'lake-manifest.json'
    if manifest.exists():
        data = json.loads(manifest.read_text())
        for package in data.get('packages', []):
            if package['name'] == name:
                if package['type'] == 'path':
                    return (project / package['dir']).resolve()
                return (project / data.get('packagesDir', '.lake/packages') / name).resolve()
    if name == 'vampire_hammer':
        sibling = project.parent / 'vampire-hammer-clean'
        if sibling.is_dir():
            return sibling.resolve()
        # Also support historical monolithic projects and synthetic fixtures.
        return project
    if name == 'tptp_parser':
        return hammer_project(project) / 'vendor/tptp_parser'
    return project / '.lake/packages' / name


def hammer_project(project=None):
    return package_root('vampire_hammer', project)


def vampire_executable(project=None):
    return hammer_project(project) / '.lake/vampire/build/vampire'


def source_provenance(project):
    """Record exact revisions and working-tree changes, including new files."""
    project = Path(project)
    metadata = project / '.benchmark-source.json'
    if metadata.exists():
        return json.loads(metadata.read_text())
    revision = subprocess.run(['git', '-C', str(project), 'rev-parse', 'HEAD'],
                              capture_output=True, text=True)
    dirty = subprocess.run(['git', '-C', str(project), 'status', '--porcelain'],
                           capture_output=True, text=True)
    return {'revision': revision.stdout.strip() if revision.returncode == 0 else None,
            'dirty': dirty.stdout.splitlines() if dirty.returncode == 0 else []}


def source_files():
    roots = {
        "hammer": hammer_project() / "VampireHammer",
        "parser": package_root("tptp_parser"),
        "auto": package_root("auto") / "Auto",
        "vamplean": package_root("vamplean"),
    }
    # Record the extracted harness, runners, analysis code, and both packages'
    # build/dependency configuration, not just the solver's Lean modules.
    for relative in ('VampireHammerBenchmark', 'scripts', 'slurmrunner'):
        roots[f'benchmark/{relative}'] = PROJECT / relative
    for label, root in roots.items():
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root)
            if any(part in {".lake", ".git", "Benchmark"} for part in relative.parts):
                continue
            if path.is_file() and (path.name == "run-hammer-task" or path.suffix in {".lean", ".c", ".h", ".l", ".y", ".toml", ".py", ".sh", ".json"}):
                yield f"{label}/{relative}", path
    for label, project in (('benchmark', PROJECT), ('hammer-project', hammer_project())):
        for relative in ('VampireHammerBenchmark.lean', 'lakefile.lean', 'lake-manifest.json', 'lean-toolchain'):
            path = project / relative
            if path.is_file():
                yield f'{label}/{relative}', path


def lean_string(value):
    """Lean string literals accept UTF-8, not JSON UTF-16 surrogate pairs."""
    return json.dumps(value, ensure_ascii=False)


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def read_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def binary_hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


# The pipeline preserves each independently searched representation beside its
# original problem.  Keep this whitelist deliberately narrow: `.stdout`,
# `.stderr`, finite-model prechecks, and unrelated files are diagnostics, not
# extra Vampire queries.  Suffixes may be nested (for example a routed branch
# can later be retried), hence the repeated group.
_QUERY_VARIANT = re.compile(
    r"(?:\.branch-\d+\.p|\.fo\.p|\.(?:ho|fo)-(?:first|second)|"
    r"\.(?:before-ho|higher-order|with-record-laws))*$")


def vampire_problem_files(problem):
    """Return every saved Vampire input for one benchmark case, once.

    Primary and structural branch inputs retain their historical ordering;
    routed/retry variants follow deterministically.  This also works when a
    run left only an archived retry input after replacing its primary file.
    """
    candidates = [path for path in problem.parent.glob(problem.name + "*")
                  if path.is_file() and _QUERY_VARIANT.fullmatch(path.name[len(problem.name):])]
    primary = [problem] if problem in candidates else []
    branches = sorted(path for path in candidates
                      if re.fullmatch(re.escape(problem.name) + r"\.branch-\d+\.p", path.name))
    first_order = [Path(str(problem) + '.fo.p')]
    first_order = [path for path in first_order if path in candidates]
    seen = set(primary + branches + first_order)
    return primary + branches + first_order + sorted(path for path in candidates if path not in seen)


def verify_vampire_binary(config):
    expected = config.get("vampire_sha256")
    if expected is None:
        raise RuntimeError("saved run has no Vampire binary fingerprint; use a new output directory")
    if binary_hash(config["vampire"]) != expected:
        raise RuntimeError("Vampire executable changed since extraction; use a new output directory")


def run_lean(source, log, timeout=600):
    with log.open("w") as stream:
        process = subprocess.Popen(
            lean_command(source), cwd=PROJECT,
            stdout=stream, stderr=subprocess.STDOUT, start_new_session=True,
        )
        try:
            code = process.wait(timeout=timeout)
        except BaseException:
            stop(process)
            raise
    if code:
        raise RuntimeError(f"Lean failed ({code}); inspect {log}")


def stop(process):
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait()


def sample(rows, count, seed):
    """Round-robin modules; seeded hash order within and between modules."""
    key = lambda text: hashlib.sha256(f"{seed}:{text}".encode()).digest()
    groups = defaultdict(list)
    for row in rows:
        groups[row["module"]].append(row)
    for group in groups.values():
        group.sort(key=lambda row: key(row["name"]), reverse=True)
    modules = sorted(groups, key=key)
    selected = []
    while modules and len(selected) < count:
        remaining = []
        for module in modules:
            selected.append(groups[module].pop())
            if groups[module]:
                remaining.append(module)
            if len(selected) == count:
                break
        modules = remaining
    return selected


def snapshot(output):
    hashes = {}
    for key, path in source_files():
        destination = output / "source-snapshot" / key
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        hashes[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    write_json(output / "source-hashes.json", hashes)
    return hashes


def extract(output, count, seed):
    catalog_source = output / "Catalog.lean"
    catalog_source.write_text(IMPORTS + 'set_option maxHeartbeats 0\n' +
                              f'hammer_mathlib_catalog {lean_string(str(output / "catalog.jsonl"))}\n')
    run_lean(catalog_source, output / "catalog.log")
    catalog = read_rows(output / "catalog.jsonl")
    if len(catalog) < count:
        raise RuntimeError(f"only {len(catalog)} eligible public mathlib theorems; requested {count}")
    selected = sample(catalog, count, seed)
    selection = output / "selection.txt"
    selection.write_text("".join(row["name"] + "\n" for row in selected))
    statements = output / "statements.jsonl"
    source = output / "Statements.lean"
    source.write_text(IMPORTS + 'set_option maxHeartbeats 0\nset_option maxRecDepth 3000\n' +
                      f'hammer_mathlib_statements {lean_string(str(selection))} to {lean_string(str(statements))}\n')
    run_lean(source, output / "statements.log")
    by_name = {row["name"]: row for row in read_rows(statements)}
    if set(by_name) != {row["name"] for row in selected}:
        raise RuntimeError("statement extraction did not reproduce the selected declarations")
    for index, row in enumerate(selected):
        row.update(by_name[row["name"]])
        row["id"] = index
    (output / "lemmas.jsonl").write_text("".join(json.dumps(row) + "\n" for row in selected))
    return selected, len(catalog)


def select_from_corpus(corpus, names, count):
    """Reuse statements and IDs for a focused comparison with an earlier run."""
    rows = read_rows(corpus / "lemmas.jsonl")
    if names:
        requested = set(names.read_text().splitlines()) - {""}
        missing = requested - {row["name"] for row in rows}
        if missing:
            raise ValueError(f"names absent from corpus: {sorted(missing)}")
        rows = [row for row in rows if row["name"] in requested]
    if not rows:
        raise ValueError("selection is empty")
    return rows[:count]


def bounded_wrapper(output, binary, memory):
    wrapper = output / "vampire-bounded.py"
    wrapper.write_text(
        "#!/usr/bin/env python3\nimport os, sys\nargs = sys.argv[1:]\n"
        "if '--forced_options' in args:\n"
        "    i = args.index('--forced_options') + 1\n"
        f"    args[i] += ':memory_limit={memory}'\n"
        f"os.execv({str(binary)!r}, ['vampire', *args, '--memory_limit', {str(memory)!r}])\n"
    )
    wrapper.chmod(0o700)
    return wrapper


def failure_kind(event):
    if event["status"] != "error":
        return event["status"]
    message = event.get("error", "").lower()
    if "heartbeats" in message:
        return "heartbeat_limit"
    if "recursion" in message:
        return "recursion_limit"
    if any(word in message for word in ("could not start vampire executable", "vampire exited with code",
                                        "vampire aborted by signal", "mock vampire failed")):
        return "process_error"
    if "vampire timed out" in message or "vampire proof-search budget exhausted" in message:
        return "search_timeout"
    if "vampire did not produce a refutation" in message:
        return "search_unsuccessful"
    # Replay errors may mention an unsupported higher-order rule or witness;
    # those are reconstruction failures, not unsupported source exports.
    if any(word in message for word in ("could not decode vampire", "could not parse vampire",
                                        "could not preprocess vampire", "vampire step", "tptp input step",
                                        "reconstruct", "skolem", "proof boundary", "replay", "certificate",
                                        "vampire proof", "decoded vampire", "difference witness",
                                        "quantifier witness", "choice operator", "difference operator",
                                        "named predicate", "predicate definition", "polarity-flipped", "avatar ")):
        return "replay_error"
    if "monomorphization" in message or "auto.reif.resolvety" in message:
        return "unsupported_input"
    if any(word in message for word in ("higher-order", "unsupported", "inhabitation", "not inhabited", "missing inhabitant")):
        return "unsupported_input"
    if event.get('prover') in {'lean-auto-duper', 'duper', 'aesop', 'simp_all', 'grind'} and any(
            word in message for word in ('unsolved goals', 'duper saturated', 'failed to prove',
                                         'duper was terminated', 'aesop: search failed', '`grind` failed')):
        return "search_unsuccessful"
    return "other_error"


def case_command(row, output, events, search_seconds=5, case_options=""):
    problem = output / "problems" / f'{row["id"]:05d}.p'
    return (case_options +
            f'set_option vampire.timeLimit {search_seconds} in\n'
            f'set_option vampire.diagnosticCase {lean_string(row["name"])} in\n'
            f'set_option vampire.outputPath {lean_string(str(problem))} in\n'
            f'hammer_mathlib_try {lean_string(row["name"])} recording {lean_string(str(events))}\n')


COMPARISON_HEARTBEAT_OPTIONS = (
    'maxHeartbeats', 'synthInstance.maxHeartbeats',
)


def case_options_prelude(pairs):
    """Format repeatable NAME=VALUE pairs as `set_option` lines for each case."""
    prelude = ""
    for pair in pairs:
        name, _, value = pair.partition("=")
        if not name or not value:
            raise SystemExit(f"--case-option expects NAME=VALUE, got {pair!r}")
        prelude += f"set_option {name} {value} in\n"
    return prelude


def run_chunk(rows, output, wrapper, timeout, worker_id, record, search_seconds=5, case_options=""):
    """Restart only the unattempted tail after a per-lemma timeout or crash."""
    remaining = list(rows)
    attempt = 0
    while remaining:
        stem = f"chunk-{worker_id:04d}-{attempt:02d}"
        events = output / "chunks" / f"{stem}.jsonl"
        source = output / "chunks" / f"{stem}.lean"
        log = output / "chunks" / f"{stem}.log"
        header = (IMPORTS + f'set_option vampire.executable {lean_string(str(wrapper))}\n'
                  'set_option Elab.async false\n')
        source.write_text(header + "".join(case_command(row, output, events, search_seconds, case_options) for row in remaining))
        by_name = {row["name"]: row for row in remaining}
        finished = set()
        active = None
        started = time.monotonic()
        active_start = started
        position = 0
        reason = None
        protocol_error = None
        terminal_seen = False
        with log.open("w") as stream:
            process = subprocess.Popen(
                lean_command(source), cwd=PROJECT,
                stdout=stream, stderr=subprocess.STDOUT, start_new_session=True,
            )
            try:
                while True:
                    if events.exists():
                        with events.open() as incoming:
                            incoming.seek(position)
                            while True:
                                previous = incoming.tell()
                                line = incoming.readline()
                                if not line.endswith("\n"):
                                    position = previous
                                    break
                                position = incoming.tell()
                                event = json.loads(line)
                                supplemental = event.get('event') in {'premises', 'proof_checked'}
                                if (event.get("name") not in by_name or
                                        event.get("event") not in {"started", "finished", "premises", "proof_checked"} or
                                        (supplemental and event.get('name') != active)):
                                    reason = "event_protocol_error"
                                    protocol_error = f"Unexpected benchmark event: {event!r}"
                                    stop(process)
                                    break
                                if supplemental:
                                    continue
                                if event["event"] == "started":
                                    if active is not None:
                                        # Lean can abort a command at its heartbeat limit
                                        # and continue with the next command without a
                                        # terminal event. Record that case immediately.
                                        record({"event": "finished", "id": by_name[active]["id"],
                                                "name": active, "status": "process_error",
                                                "error": "Lean started the next case without completing this case",
                                                "seconds": round(time.monotonic() - active_start, 3),
                                                "log": str(log), "source": str(source),
                                                "kernel_checked": False})
                                        finished.add(active)
                                    active = event["name"]
                                    active_start = time.monotonic()
                                else:
                                    event.update({"id": by_name[event["name"]]["id"], "log": str(log), "source": str(source)})
                                    record(event)
                                    finished.add(event["name"])
                                    active = None
                                    active_start = time.monotonic()
                    if reason == "event_protocol_error":
                        break
                    code = process.poll()
                    if code is not None:
                        if not terminal_seen:
                            # Drain final events after observing process exit.
                            terminal_seen = True
                            continue
                        if len(finished) < len(remaining):
                            reason = "process_error"
                        break
                    elapsed = time.monotonic() - active_start
                    # Import time is separate from the per-lemma budget.
                    if elapsed > (timeout if active else 180):
                        reason = "timeout" if active else "startup_timeout"
                        stop(process)
                        break
                    time.sleep(0.2)
            except BaseException:
                stop(process)
                raise
        remaining = [row for row in remaining if row["name"] not in finished]
        if reason and remaining:
            failed = next((row for row in remaining if row["name"] == active), remaining[0])
            record({"event": "finished", "id": failed["id"], "name": failed["name"],
                    "status": "infrastructure_error" if reason == "event_protocol_error" else reason,
                    **({"error": protocol_error} if protocol_error else {}),
                    "seconds": round(time.monotonic() - active_start, 3),
                    "log": str(log), "source": str(source), "returncode": process.returncode,
                    "kernel_checked": False})
            remaining.remove(failed)
            if reason == "startup_timeout" or (reason == "process_error" and active is None):
                # An import/infrastructure failure affects the entire tail.
                for row in remaining:
                    record({"event": "finished", "id": row["id"], "name": row["name"],
                            "status": "infrastructure_error", "log": str(log), "source": str(source),
                            "kernel_checked": False})
                remaining = []
        attempt += 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=5000)
    parser.add_argument("--seed", default="mathlib-v4.29.0-hammer-20261007")
    parser.add_argument("--vampire", default=str(vampire_executable()))
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--chunk-size", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=30, help="wall seconds per lemma, excluding imports")
    parser.add_argument("--search-seconds", type=int, default=5, help="Vampire search budget per lemma; resume keeps its saved budget")
    parser.add_argument("--memory-mib", type=int, default=512)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--extract-only", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--corpus", type=Path, help="reuse statements from an extracted corpus")
    parser.add_argument("--names", type=Path, help="select newline-separated names from --corpus")
    parser.add_argument("--case-option", action="append", default=[], metavar="NAME=VALUE",
                        help="extra 'set_option NAME VALUE in' line prepended to every benchmark case (repeatable)")
    args = parser.parse_args()
    if min(args.count, args.jobs, args.chunk_size, args.timeout, args.memory_mib, args.search_seconds) <= 0:
        parser.error("counts, concurrency, and resource limits must be positive")
    if args.names and not args.corpus:
        parser.error("--names requires --corpus")
    if args.resume and args.corpus:
        parser.error("--resume uses its saved selection; omit --corpus")
    binary = shutil.which(args.vampire)
    if binary is None:
        parser.error(f"Vampire executable not found: {args.vampire}")
    output = args.output or PROJECT / "artifacts" / datetime.datetime.now().strftime("mathlib-corpus-%Y%m%d-%H%M%S")
    if not output.is_absolute():
        output = PROJECT / output
    output = output.resolve()
    if args.resume:
        rows = read_rows(output / "lemmas.jsonl")
        config = json.loads((output / "config.json").read_text())
        verify_vampire_binary(config)
        expected = json.loads((output / "source-hashes.json").read_text())
        actual = {key: hashlib.sha256(path.read_bytes()).hexdigest() for key, path in source_files()}
        if actual != expected:
            raise RuntimeError("hammer/dependency sources changed since extraction; use a new output directory")
    else:
        output.mkdir(parents=True, exist_ok=False)
        config = {"count": args.count, "seed": args.seed, "vampire": str(Path(binary).resolve()),
                  "jobs": args.jobs, "chunk_size": args.chunk_size, "timeout": args.timeout,
                  "memory_mib": args.memory_mib, "search_seconds": args.search_seconds, "hammer_changes": False,
                  "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
        config["vampire_sha256"] = binary_hash(config["vampire"])
        config["case_options"] = case_options_prelude(args.case_option)
        config["mathlib_commit"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=PROJECT / ".lake/packages/mathlib", text=True).strip()
        config["lean_version"] = subprocess.check_output(
            ["lake", "-R", "env", "lean", "--version"], cwd=PROJECT, text=True).strip()
        write_json(output / "config.json", config)
        snapshot(output)
        if args.corpus:
            original_config = json.loads((args.corpus / "config.json").read_text())
            if original_config["mathlib_commit"] != config["mathlib_commit"]:
                raise RuntimeError("the corpus and current mathlib revision differ")
            rows = select_from_corpus(args.corpus, args.names, args.count)
            candidates = original_config["eligible_theorems"]
            config.update({"count": len(rows), "corpus": str(args.corpus.resolve()),
                           "names": str(args.names.resolve()) if args.names else None})
            (output / "lemmas.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
            (output / "selection.txt").write_text("".join(row["name"] + "\n" for row in rows))
        else:
            rows, candidates = extract(output, args.count, args.seed)
        config.update({"eligible_theorems": candidates, "selected_modules": len({row["module"] for row in rows})})
        write_json(output / "config.json", config)
    print(f"Extracted {len(rows)} statements across {len({row['module'] for row in rows})} modules: {output}", flush=True)
    if args.extract_only:
        return 0
    for directory in ("cases", "chunks", "problems"):
        (output / directory).mkdir(exist_ok=True)
    if "mathlib_commit" not in config:
        config["mathlib_commit"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=PROJECT / ".lake/packages/mathlib", text=True).strip()
    if "lean_version" not in config:
        config["lean_version"] = subprocess.check_output(
            ["lake", "-R", "env", "lean", "--version"], cwd=PROJECT, text=True).strip()
    wrapper = bounded_wrapper(output, config["vampire"], config["memory_mib"])
    harness = PROJECT / "VampireHammerBenchmark/Mathlib.lean"
    harness_hash = hashlib.sha256(harness.read_bytes()).hexdigest()
    if config.get("harness_sha256", harness_hash) != harness_hash and (output / "results.jsonl").exists():
        raise RuntimeError("benchmark harness changed; do not mix results from different harnesses")
    shutil.copy2(harness, output / "benchmark-harness.lean")
    config["harness_sha256"] = harness_hash
    write_json(output / "config.json", config)
    for row in rows:
        path = output / "cases" / f'{row["id"]:05d}.lean'
        path.write_text(IMPORTS + f'set_option vampire.executable {lean_string(str(wrapper))}\n' +
                        case_command(row, output, output / "cases" / f'{row["id"]:05d}.jsonl', config.get("search_seconds", 5), config.get("case_options", "")))
    results = output / "results.jsonl"
    completed = {row["id"]: row for row in read_rows(results)} if results.exists() else {}
    pending = [row for row in rows if row["id"] not in completed]
    lock = threading.Lock()
    started = time.monotonic()

    def record(event):
        with lock:
            if event["id"] in completed:
                raise RuntimeError(f"duplicate benchmark result for {event['id']}")
            event["failure_kind"] = failure_kind(event)
            problem = output / "problems" / f'{event["id"]:05d}.p'
            # Structural preparation can prove several first-order branches.
            # Retain every query; keep the legacy primary link for analysis.
            queries = vampire_problem_files(problem)
            event["problems"] = [str(query) for query in queries]
            event["problem"] = str(queries[0]) if queries else None
            if queries:
                problem = queries[0]
            exitfile = Path(str(problem) + ".exitcode")
            try:
                event["vampire_exit"] = int(exitfile.read_text()) if exitfile.exists() else None
            except ValueError:
                event["vampire_exit"] = None
            stdout = Path(str(problem) + ".stdout")
            if stdout.exists():
                text = stdout.read_text()
                times = re.findall(r"% Time elapsed: ([0-9.]+) s", text)
                event["search_seconds"] = float(times[-1]) if times else None
                event["stdout"] = str(stdout)
            completed[event["id"]] = event
            with results.open("a") as stream:
                stream.write(json.dumps(event) + "\n")
            counts = dict(Counter(row["status"] for row in completed.values()))
            summary = {"selected": len(rows), "attempted": len(completed), "status_counts": counts,
                       "failure_kinds": dict(Counter(row.get("failure_kind", row["status"]) for row in completed.values())),
                       "all_attempted": len(completed) == len(rows), "run_wall_seconds": round(time.monotonic() - started, 3)}
            write_json(output / "summary.json", summary)
            if len(completed) <= 10 or len(completed) % 50 == 0:
                print(f"{len(completed)}/{len(rows)}: {counts}", flush=True)

    chunks = [pending[index:index + config["chunk_size"]] for index in range(0, len(pending), config["chunk_size"])]
    batch_id = max((int(path.stem.split('-')[1]) for path in (output / "chunks").glob("chunk-*.lean")), default=-1) + 1
    with ThreadPoolExecutor(max_workers=config["jobs"]) as workers:
        futures = [workers.submit(run_chunk, chunk, output, wrapper, config["timeout"], batch_id + index, record, config.get("search_seconds", 5), config.get("case_options", ""))
                   for index, chunk in enumerate(chunks)]
        for future in as_completed(futures):
            future.result()
    if (output / "summary.json").exists():
        summary = json.loads((output / "summary.json").read_text())
        summary["vampire_binary_unchanged"] = binary_hash(config["vampire"]) == config["vampire_sha256"]
        write_json(output / "summary.json", summary)
        if not summary["vampire_binary_unchanged"]:
            raise RuntimeError("Vampire executable changed during this run; results may mix different builds")
    print(f"Finished: {output / 'summary.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
