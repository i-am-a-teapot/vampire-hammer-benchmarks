#!/usr/bin/env python3
"""Run isolated prover benchmarks locally; shared helpers for source replay."""

from collections import defaultdict
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys

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
    if name == 'tptp_parser':
        return hammer_project(project) / 'vendor/tptp_parser'
    raise ValueError(f'dependency {name!r} is not recorded in {manifest}')


def hammer_project(project=None):
    return package_root('vampire_hammer', project)


def vampire_executable(project=None):
    return hammer_project(project) / '.lake/vampire/build/vampire'


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
            f'hammer_mathlib_try_with_premises {lean_string(row["name"])} recording {lean_string(str(events))}\n')


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


def main():
    spec = importlib.util.spec_from_file_location('isolated_local', PROJECT / 'scripts/hammer_bench/backends.py')
    isolated = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(isolated)
    return isolated.local_main(sys.argv[1:], PROJECT)


if __name__ == "__main__":
    raise SystemExit(main())
