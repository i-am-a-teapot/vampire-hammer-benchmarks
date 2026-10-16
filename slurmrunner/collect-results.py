#!/usr/bin/env python3
"""Summarize hammer results from BenchExec log files.

Reads the `HAMMER-RESULT:` line of every run log found below the given paths
(directories, BenchExec `*.logfiles.zip` archives or single `.log` files), so
it works independently of the result-XML format of a particular BenchExec-like
tool. Runs killed by the benchmarking tool have no such line and are reported
as `killed_by_limit`. With --suite, theorems without any log are listed as
missing, which shows which shards still need to be (re)run.
"""

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import re
import sys
import zipfile
import importlib.util

_spec = importlib.util.spec_from_file_location('slurm_results', Path(__file__).with_name('slurmbench.py'))
slurm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(slurm)

RESULT_PREFIX = "HAMMER-RESULT: "
THEOREM_PREFIX = "[run-hammer-task] theorem "
# BenchExec names logs <rundefinition>.<task file>.log; task files start with a six-digit id.
LOG_NAME = re.compile(r"^(?:.*?\.)?(?P<run>[^.]+)\.(?P<id>\d{6})-[^/]*\.log$")


def log_texts(paths):
    for path in paths:
        if path.is_dir():
            for child in sorted(path.rglob("*")):
                if child.suffix == ".log" and child.is_file():
                    yield child.name, child.read_text(errors="replace")
                elif child.suffix == ".zip":
                    yield from zip_texts(child)
        elif path.suffix == ".zip":
            yield from zip_texts(path)
        else:
            yield path.name, path.read_text(errors="replace")


def zip_texts(path):
    with zipfile.ZipFile(path) as archive:
        for member in archive.namelist():
            if member.endswith(".log"):
                yield Path(member).name, archive.read(member).decode(errors="replace")


def parse_log(name, text):
    match = LOG_NAME.match(name)
    if match is None:
        return None
    row = None
    theorem = None
    for line in text.splitlines():
        if line.startswith(THEOREM_PREFIX):
            theorem = line[len(THEOREM_PREFIX):].split(";")[0]
        elif line.startswith(RESULT_PREFIX):
            try:
                candidate = json.loads(line[len(RESULT_PREFIX):])
                if isinstance(candidate, dict):
                    row = candidate
            except json.JSONDecodeError:
                pass
    if row is None:
        row = {"name": theorem, "status": "killed_by_limit", "failure_kind": "killed_by_limit",
               "error": "no HAMMER-RESULT line: the run was killed (time/memory limit) or crashed"}
    row.update({"id": int(match["id"]), "rundefinition": match["run"], "log": name})
    row.update(failure_details(slurm.hammer_metrics(text, None)))
    return row


def failure_details(metrics):
    """Use the same diagnostics for legacy logs and Slurm result journals."""
    if metrics.get('prover', 'vampire-hammer') != 'vampire-hammer':
        return {}
    details = {'lean_errors': metrics.get('lean-errors', []),
            'last_phase': metrics.get('last-phase'), 'phases': metrics.get('phases', {}),
            'query_details': metrics.get('query-details', []),
            'attempt_events': metrics.get('attempt-events', []), 'log_tail': metrics.get('log-tail')}
    error = metrics.get('error-full') or metrics.get('error')
    if error:
        details['error'] = error
    return details


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path, help="BenchExec results directories, logfile zips or logs")
    parser.add_argument("--suite", type=Path, help="suite directory, to report missing theorems")
    parser.add_argument("--jsonl", type=Path, help="write one merged result row per run")
    args = parser.parse_args()

    runs = defaultdict(dict)
    legacy_paths = []
    for path in args.paths:
        manifest = path / 'manifest.json' if path.is_dir() else path
        if manifest.name == 'manifest.json' and manifest.is_file():
            cfg = json.loads(manifest.read_text())
            for idx, data in slurm.read_results(cfg['output'], bulk_only=cfg.get('result_format') == 'bulk').items():
                job = cfg['jobs'][idx]
                x = data.get('extracted') or {}
                theorem_id = int(job['input'].rsplit('/', 1)[-1].split('#')[-1].split('-')[0])
                status = 'proved' if data['status'] == 'true' else x.get('outcome', data['status'])
                row = {'id': theorem_id, 'name': x.get('theorem') or job.get('theorem'),
                       'status': status, 'failure_kind': x.get('outcome', status), 'route': x.get('route'),
                       'rundefinition': job['runset'], 'log': data.get('log_storage') or f'logs/{idx:06d}.log'}
                if args.jsonl:
                    # Full event export remains available; ordinary summaries
                    # need only the compact metadata, without reading logs.
                    text = slurm.read_run_log(cfg['output'], data)
                    metrics = slurm.hammer_metrics(text, data.get('returncode'), data.get('reason') == 'TIMEOUT',
                                                  prover=slurm.job_prover(job))
                    row.update(failure_details(metrics))
                    row.update({'returncode': data.get('returncode'),
                                'termination_reason_runner': data.get('reason')})
                    for line in text.splitlines():
                        if line.startswith(RESULT_PREFIX):
                            try:
                                event = json.loads(line[len(RESULT_PREFIX):])
                            except json.JSONDecodeError:
                                continue
                            row.update(event)
                    if data.get('reason') == 'TIMEOUT':
                        row.update(status=metrics['outcome'], failure_kind=metrics['outcome'])
                    row.update(id=theorem_id, rundefinition=job['runset'], log=data.get('log_storage') or f'logs/{idx:06d}.log')
                runs[job['runset']][theorem_id] = row
        else:
            legacy_paths.append(path)
    for name, text in log_texts(legacy_paths):
        row = parse_log(name, text)
        if row is not None:
            # A rerun of the same task supersedes an earlier (e.g. killed) one.
            runs[row["rundefinition"]][row["id"]] = row
    if not runs:
        sys.exit("no hammer run logs found")
    theorems = None
    if args.suite:
        theorems = {row["id"]: row for row in map(json.loads, (args.suite / "theorems.jsonl").read_text().splitlines())}

    for rundefinition, rows in sorted(runs.items()):
        kinds = Counter(row.get("failure_kind", row["status"]) for row in rows.values())
        proved = [row for row in rows.values() if row["status"] == "proved"]
        routes = Counter(row.get("route") for row in proved)
        total = len(theorems) if theorems else len(rows)
        excluded = sum(row.get("status") == "excluded" for row in rows.values())
        eligible = total - excluded
        print(f"## {rundefinition}: {len(rows)} runs" + (f" of {total} theorems" if theorems else ""))
        print()
        print("| Outcome | Count |\n| --- | ---: |")
        for kind, count in kinds.most_common():
            print(f"| `{kind}` | {count} |")
        if theorems:
            print(f"| missing (no log) | {len(set(theorems) - set(rows))} |")
        print()
        print(f"Independent kernel-checked proofs: **{len(proved)}/{eligible}** eligible "
              f"({100 * len(proved) / eligible if eligible else 0:.2f}%); direct Lean closure {routes['direct']}, "
              f"Vampire reconstruction {routes['vampire']}.\n")
        if excluded:
            print(f"Excluded from the denominator: {excluded} theorems.\n")
        if theorems:
            missing = sorted(set(theorems) - set(rows))
            if missing:
                shards = sorted({f'{theorems[index]["shard"]:04d}' if 'shard' in theorems[index]
                                 else theorems[index]["task"].split("/")[1] for index in missing})
                print(f"Shards with missing runs: {', '.join('shard-' + shard for shard in shards[:40])}"
                      + (" ..." if len(shards) > 40 else "") + "\n")
    if args.jsonl:
        with args.jsonl.open("w") as stream:
            for rows in runs.values():
                for row in sorted(rows.values(), key=lambda row: row["id"]):
                    stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
