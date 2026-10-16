#!/usr/bin/env python3
"""Simple XML -> Slurm array/packed job -> BenchExec table-generator XML bridge.
Only stdlib required. Run `python slurmbench.py --help`.

Tool modes (the `tool` attribute of <benchmark>):
  vampire   status and columns as BenchExec's Vampire tool-info module;
  *hammer*  the Vampire hammer on mathlib (slurmrunner/run-hammer-task, e.g.
            tool="hammer_bench.tool_info"): status as its BenchExec tool-info
            module, plus failure classification and diagnostics columns, and
            `collect` writes summary.json, cases.tsv and per-category theorem lists;
  other     status from statusRegex, else DONE/ERROR by exit code.
"""
import argparse
from collections import Counter, defaultdict
import datetime as dt
import errno
from concurrent.futures import ThreadPoolExecutor
import math
import shutil
import glob
import gzip
import json
import os
from pathlib import Path
import re
import resource
import shlex
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import uuid
import time
import xml.etree.ElementTree as ET


def seconds(raw):
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smh]?)\s*", raw)
    if not m:
        raise ValueError(f"Invalid time duration: {raw!r} (use 60s, 5m, or 1h)")
    return float(m[1]) * {'': 1, 's': 1, 'm': 60, 'h': 3600}[m[2]]


def memory_mb(raw):
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(MB|GB|MiB|GiB)?\s*", raw, re.I)
    if not m:
        raise ValueError(f"Invalid memlimit: {raw!r} (use 1000 MB or 4 GB)")
    n, unit = float(m[1]), (m[2] or 'MB').lower()
    return max(1, int(n * {'mb': 1, 'gb': 1000, 'mib': 1.048576, 'gib': 1073.741824}[unit] + .999999))


def opts(parent):
    result = []
    for elem in parent.findall('option'):
        if elem.get('name'):
            result.append(elem.get('name'))
        value = (elem.text or '').strip()
        if value:
            # Unlike shell, option text is one argument; use <option> per arg.
            result.append(value)
    return result


def tool_kind(cfg):
    tool = cfg.get('tool', 'generic')
    if tool == 'vampire':
        return 'vampire'
    if 'hammer' in tool.lower():
        return 'hammer'
    return 'generic'


def parse_xml(path):
    root = ET.parse(path).getroot()
    if root.tag != 'benchmark':
        raise ValueError('Expected <benchmark> root')
    exe = root.get('command')
    if not exe:
        raise ValueError('Missing command="/path/to/program" on <benchmark>')
    base = path.parent.resolve()
    workdir = (base / root.get('workdir', '.')).resolve()
    if not workdir.is_dir():
        raise ValueError(f'Working directory does not exist: {workdir}')
    run_defs = root.findall('rundefinition') or [ET.Element('rundefinition', {'name': 'default'})]
    tasks = []
    catalogs = {}
    compact_tasks = []

    def expand_set_file(setfile):
        """Expand BenchExec-style set files, relative to the set file's directory."""
        if not setfile.is_file():
            raise ValueError(f'Set file does not exist: {setfile}')
        result = []
        for line_no, line in enumerate(setfile.read_text().splitlines(), 1):
            pattern = line.strip()
            if not pattern or pattern.startswith(('#', '%')):
                continue
            found = glob.glob(str(setfile.parent / pattern), recursive=True)
            if not found:
                print(f'Warning: no matches for {pattern!r} in {setfile}:{line_no}', file=sys.stderr)
            result.extend(found)
        return result

    for block in root.findall('tasks'):
        matches = []
        for include in block.findall('include'):
            pattern = (include.text or '').strip()
            compact = re.fullmatch(r'(.+\.jsonl)#(\d+):(\d+)', pattern)
            if compact and tool_kind({'tool': root.get('tool', '')}) == 'hammer':
                filename = (base / compact[1]).resolve()
                if filename not in catalogs:
                    with filename.open() as stream:
                        catalogs[filename] = {row['id']: row for row in map(json.loads, stream)}
                for ident in range(int(compact[2]), int(compact[3])):
                    row = catalogs[filename].get(ident)
                    if row:
                        compact_tasks.append((block.get('name') or 'tasks',
                                              f'{filename}#{row["task"].split("#", 1)[1]}', row['name'], row.get('module')))
                continue
            for filename in glob.glob(str(base / pattern), recursive=True):
                if Path(filename).suffix.lower() == '.set':
                    matches.extend(expand_set_file(Path(filename)))
                else:
                    matches.append(filename)
        for includesfile in block.findall('includesfile'):
            pattern = (includesfile.text or '').strip()
            setfiles = glob.glob(str(base / pattern), recursive=True)
            if not setfiles:
                raise ValueError(f'No set files match <includesfile>{pattern}</includesfile>')
            for filename in setfiles:
                matches.extend(expand_set_file(Path(filename)))
        for item in block.findall('withoutfile'):
            raise ValueError('withoutfile is not supported; specify input files with <include>')
        for filename in sorted(set(matches)):
            p = Path(filename)
            if p.is_file():
                tasks.append((block.get('name') or 'tasks', str(p.resolve())))
    if not tasks and not compact_tasks:
        raise ValueError('No matching input files found in <tasks><include>')
    jobs = []
    for rd in run_defs:
        for taskset, inp in tasks:
            jobs.append({'runset': rd.get('name') or 'default', 'taskset': taskset, 'input': inp,
                         'args': opts(root) + opts(rd) + [inp]})
        for taskset, inp, theorem, module in compact_tasks:
            jobs.append({'runset': rd.get('name') or 'default', 'taskset': taskset, 'input': inp,
                         'theorem': theorem, 'args': opts(root) + opts(rd) +
                         (['--module', module] if module else []) + ['--theorem', theorem]})
    return {'name': path.stem, 'executable': str((base / exe).resolve()) if '/' in exe else exe,
            'workdir': str(workdir), 'jobs': jobs,
            'cpus': int(root.get('cpuCores', '1')),
            'mem_mb': memory_mb(root.get('memlimit', '1024 MB')),
            'seconds': seconds(root.get('timelimit', '60s')),
            'status_regex': root.get('statusRegex', ''),
            'tool': root.get('tool', 'generic')}


def vampire_metrics(text, exitcode, timed_out=False):
    """Match BenchExec's benchexec.tools.vampire result extraction."""
    lines = text.splitlines()
    def unique_word(prefix, index):
        matches = [line.split() for line in lines if line.startswith(prefix)]
        if len(matches) != 1 or len(matches[0]) <= index:
            return None
        return matches[0][index]

    szs = unique_word('% SZS status', 3)
    instructions = unique_word('% Instructions burned', 3)
    reasons = [line[len('% Termination reason: '):] for line in lines
               if line.startswith('% Termination reason: ')]
    if timed_out or szs == 'Timeout':
        status = 'TIMEOUT'
    elif exitcode is None:
        status = 'ERROR'
    elif exitcode != 0:
        if exitcode == 4 and text.startswith('Parsing Error'):
            status = 'PARSING ERROR'
        elif exitcode == 1 and text.startswith('% Exception at proof search level\n') and any(
                line.startswith('Parsing Error') for line in lines):
            status = 'PARSING ERROR'
        elif exitcode == 1 and reasons == ['Time limit']:
            status = 'TIMEOUT'
        elif exitcode == 1 and reasons == ['Instruction limit']:
            status = 'OUT OF INSTRUCTIONS'
        elif exitcode == 1 and reasons == ['Memory limit']:
            status = 'OUT OF MEMORY'
        elif exitcode == 1 and reasons in (
                ['Refutation not found, incomplete strategy'],
                ['Refutation not found, non-redundant clauses discarded']):
            status = 'INCOMPLETE'
        else:
            status = 'ERROR'
    elif szs in ('ContradictoryAxioms', 'Theorem', 'Unsatisfiable'):
        status = 'true'
    elif szs in ('CounterSatisfiable', 'Satisfiable'):
        status = 'false'
    else:
        status = 'unknown'
    return {'status': status, 'szs-status': szs,
            'instruction-count': instructions if instructions is not None else '0',
            'termination-reasons': reasons}


# --- Vampire hammer on mathlib (slurmrunner/run-hammer-task) -------------------

HAMMER_RESULT = 'HAMMER-RESULT: '
HAMMER_THEOREM = '[run-hammer-task] theorem '
# Expected limitations of the hammer rather than malfunctions; the BenchExec
# tool-info module reports these as `unknown` as well.
HAMMER_UNKNOWN = {'search_timeout', 'search_unsuccessful', 'unsupported_input', 'proof_timeout', 'excluded'}
# A run killed at the deadline or without a parseable result measured nothing;
# a resumed run repeats it instead of skipping it.
HAMMER_INCOMPLETE = {'slurmbench_timeout', 'no_result'}
HAMMER_COLUMNS = ['outcome', 'category', 'route', 'prover', 'kernel-checked', 'hammer-seconds', 'startup-seconds', 'replay-seconds', 'vampire-seconds',
                  'vampire-exit', 'szs-status', 'termination-reason', 'queries', 'problem-bytes',
                  'assertions', 'last-phase', 'theorem', 'lean-error', 'error', 'route-source', 'reconstruction']
COMPARISON_COLUMNS = ['outcome', 'route', 'prover', 'kernel-checked', 'hammer-seconds',
                      'startup-seconds', 'replay-seconds', 'theorem']


def usable_results(done):
    """Drop runs whose latest record measured nothing: a batch killed at the
    deadline leaves no usable result and must run again on resume."""
    return {idx for idx, row in done.items()
            if (row.get('extracted') or {}).get('outcome') not in HAMMER_INCOMPLETE}


def job_prover(job):
    args = job['args']
    return args[args.index('--prover') + 1] if '--prover' in args else 'vampire-hammer'


def hammer_category(row, lean_error=''):
    """Finer failure category, as scripts/analyze-mathlib-benchmark.py."""
    kind = row.get('failure_kind') or row.get('status')
    message = (row.get('error') or '').lower()
    failure_expression = message.split('this is because', 1)[0]
    if 'auto.lamreif.reiftermchecktype' in message:
        return 'reification_type_error'
    if kind == 'excluded':
        return row.get('exclusion_reason') or 'excluded'
    if kind == 'unsupported_input':
        if 'depends on bound variables' in failure_expression or 'of λ binder contains bound variables' in failure_expression:
            return 'dependent_binder'
        if '_proof_' in failure_expression:
            return 'proof_argument_monomorphization'
        if 'expression `inst' in failure_expression:
            return 'indexed_instance_monomorphization'
        if 'expression `match ' in failure_expression:
            return 'match_monomorphization'
        if 'unsupported domain auto.embedding.lam.lamsort.base (.int)' in message:
            return 'integer_domain'
        if 'inhabitation' in message or 'not inhabited' in message:
            return 'missing_domain_witness'
        if 'higher-order' in message or 'function type' in message:
            return 'higher_order'
        if 'monomorphization' in message or 'resolvety' in message:
            return 'other_monomorphization'
        return 'other_unsupported_export'
    lean_error = lean_error.lower()
    if 'heartbeats' in message or 'heartbeats' in lean_error:
        return 'heartbeat_limit'
    if 'proof-search budget exhausted' in message:
        return 'search_budget_exhausted'
    if 'recursion' in message or 'recursion' in lean_error:
        return 'recursion_limit'
    if row.get('termination_reason') == 'Memory limit':
        return 'vampire_memory_limit'
    if kind == 'search_unsuccessful':
        satisfiable = row.get('szs_status') in ('Satisfiable', 'CounterSatisfiable')
        return 'satisfiable' if satisfiable or 'szs status satisfiable' in message else 'other_search_unsuccessful'
    return kind


def hammer_phases(lines):
    """Read phase timings from legacy benchmark logs."""
    durations = defaultdict(float)
    open_phases, last = [], None
    for line in lines:
        if not line.startswith('[vampire.profile] '):
            continue
        body = line[len('[vampire.profile] '):].strip()
        if body.startswith('starting '):
            open_phases.append(body[len('starting '):])
            continue
        m = re.match(r'(\d+) ms: (.+)$', body)
        if not m:
            continue
        phase = m[2].strip()
        last = phase
        if phase in open_phases:
            del open_phases[len(open_phases) - 1 - open_phases[::-1].index(phase)]
        # Per-node replay and per-clause CNF spans overlap their parents.
        key = 'replay nodes' if phase.startswith('node ') else None if phase.startswith('cnfify ') else phase
        if key:
            durations[key] += int(m[1]) / 1000
    return dict(durations), (open_phases[-1] if open_phases else None), last


def shorten(text, limit=300):
    text = ' '.join((text or '').split())
    return text if len(text) <= limit else text[:limit - 3] + '...'


def hammer_metrics(text, exitcode, timed_out=False, prover=None):
    """Status as hammer_bench/tool_info.py, plus diagnostics for failures."""
    lines = text.splitlines()
    row, theorem = None, None
    for line in lines:
        if line.startswith(HAMMER_THEOREM):
            theorem = line[len(HAMMER_THEOREM):].split(';')[0]
            match = re.search(r'; prover ([^;]+)', line)
            if prover is None and match:
                prover = match[1]
        elif line.startswith(HAMMER_RESULT):
            try:
                candidate = json.loads(line[len(HAMMER_RESULT):])
                if isinstance(candidate, dict):
                    row = candidate
            except json.JSONDecodeError:
                pass
    lean_errors = []
    collecting_error = False
    for line in lines:
        match = re.match(r'^.*\.lean:\d+:\d+: error: (.*)', line)
        if match:
            lean_errors.append(match[1])
        elif line.startswith(('[vampire.', HAMMER_RESULT, HAMMER_THEOREM)) or re.match(
                r'^.*\.lean:\d+:\d+: (?:warning|info):', line):
            # Messages after this boundary are not part of the preceding error.
            collecting_error = False
            continue
        elif lean_errors and collecting_error:
            lean_errors[-1] += '\n' + line
        else:
            continue
        collecting_error = True
    lean_error = lean_errors[0] if lean_errors else ''
    phases, unfinished, last_phase = hammer_phases(lines)
    prover = prover or (row or {}).get('prover', 'vampire-hammer')
    if row is None or timed_out:
        # Killed at the time limit, by Slurm (e.g. out of memory), or crashed.
        outcome = ('slurmbench_timeout' if timed_out else
                   f'killed_by_signal_{-exitcode}' if exitcode is not None and exitcode < 0 else 'no_result')
        status = 'TIMEOUT' if timed_out else f'ERROR ({outcome})'
        row = {'name': theorem, 'failure_kind': outcome}
        category = 'heartbeat_limit' if 'heartbeats' in lean_error.lower() else outcome
    else:
        outcome = row.get('failure_kind', row.get('status'))
        status = ('true' if row.get('status') == 'proved' else
                  'unknown' if outcome in HAMMER_UNKNOWN else f'ERROR ({outcome})')
        category = hammer_category(row, lean_error)
    startup = None
    if row.get('wall_seconds') is not None and row.get('seconds') is not None:
        startup = round(row['wall_seconds'] - row['seconds'], 3)
    metrics = {'status': status, 'outcome': outcome, 'category': category,
            'prover': prover, 'kernel-checked': row.get('kernel_checked'),
            'route': row.get('route'), 'hammer-seconds': row.get('seconds'), 'startup-seconds': startup,
            'replay-seconds': row.get('replay_seconds'),
            'vampire-seconds': row.get('search_seconds'), 'vampire-exit': row.get('vampire_exit'),
            'szs-status': row.get('szs_status'), 'termination-reason': row.get('termination_reason'),
            'queries': row.get('vampire_queries'), 'problem-bytes': row.get('problem_bytes'),
            'assertions': row.get('assertions'),
            # Where a killed run was: the innermost phase that never completed.
            'last-phase': unfinished or last_phase, 'theorem': row.get('name') or theorem,
            'lean-error': shorten(lean_error), 'error': shorten((row.get('error') or '').split('\n')[0]),
            'axioms': row.get('axioms'), 'reuses-target': row.get('reuses_target'),
            'record-retry': row.get('record_retry'), 'phases': phases,
            'route-source': row.get('route_source'), 'reconstruction-modes': row.get('reconstruction_modes'),
            'reconstruction': ','.join(row.get('reconstruction_modes') or []),
            'error-full': row.get('error'), 'lean-errors': lean_errors,
            'query-details': row.get('query_details', []), 'artifacts-dir': row.get('artifacts_dir'),
            'attempt-events': row.get('attempt_events') if isinstance(row.get('attempt_events'), list)
            else attempt_events(lines, row.get('name') or theorem),
            'log-tail': text[-4096:] if status != 'true' else None}
    if prover != 'vampire-hammer':
        return {key: value for key, value in metrics.items() if key in {'status', *COMPARISON_COLUMNS}}
    return metrics


def attempt_events(lines, name):
    events = []
    for line in lines:
        if not line.startswith('[vampire.event] '):
            continue
        try:
            event = json.loads(line[len('[vampire.event] '):])
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get('name') == name:
            events.append(event)
    return events


def distribution(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return {'count': 0}
    return {'count': len(values), 'sum': round(sum(values), 3), 'median': round(statistics.median(values), 3),
            'p90': round(values[max(0, (9 * len(values) + 9) // 10 - 1)], 3), 'max': round(values[-1], 3)}


def message_pattern(text):
    """Group similar errors: drop numbers, quoted terms and long tails."""
    text = re.sub(r'`[^`]*`', '`…`', text or '')
    text = re.sub(r'\d+(\.\d+)?', 'N', text)
    return text[:160]


def hammer_diagnostics(out, runset, runs):
    """Compact summaries plus full failure details and log references."""
    target = out / 'diagnostics' / re.sub(r'[^A-Za-z0-9_.-]+', '_', runset)
    (target / 'categories').mkdir(parents=True, exist_ok=True)
    rows = [(job, data, data.get('extracted') or {}) for job, data, _ in runs]
    proved = [x for _, _, x in rows if x.get('status') == 'true']
    excluded = sum(x.get('outcome') == 'excluded' for _, _, x in rows)
    eligible = len(rows) - excluded
    by_category = defaultdict(list)
    for job, data, x in rows:
        by_category[x.get('category') or data['status']].append((job, data, x))
    phase_values = defaultdict(list)
    for _, _, x in rows:
        for phase, value in (x.get('phases') or {}).items():
            phase_values[phase].append(value)
    summary = {
        'runset': runset, 'runs': len(rows),
        'excluded': excluded, 'eligible_runs': eligible,
        'proved': len(proved), 'proved_rate': round(len(proved) / eligible, 5) if eligible else 0,
        'proved_by_route': dict(Counter(x.get('route') for x in proved)),
        'proved_by_reconstruction': dict(Counter(
            x.get('reconstruction') or ('direct' if x.get('route') == 'direct' else 'unknown')
            for x in proved)),
        'status': dict(Counter(data['status'] for _, data, _ in rows).most_common()),
        'outcome': dict(Counter(x.get('outcome') for _, _, x in rows).most_common()),
        'category': {category: len(items) for category, items in
                     sorted(by_category.items(), key=lambda item: -len(item[1]))},
        'unfinished_phase_of_timeouts': dict(Counter(
            x.get('last-phase') for _, data, x in rows if data['status'] == 'TIMEOUT').most_common()),
        'szs_status_of_search_failures': dict(Counter(
            x.get('szs-status') for _, _, x in rows if x.get('outcome') in ('search_timeout', 'search_unsuccessful')).most_common()),
        'nonstandard_axioms_in_proofs': sorted({a for x in proved for a in (x.get('axioms') or [])}
                                               - {'propext', 'Classical.choice', 'Quot.sound'}),
        'walltime_by_outcome': {k: distribution([data['walltime'] for _, data, x in rows if x.get('outcome') == k])
                                for k in sorted({x.get('outcome') for _, _, x in rows}, key=str)},
        'startup_seconds': distribution([x.get('startup-seconds') for _, _, x in rows]),
        'memory_bytes': distribution([data.get('memory') for _, data, _ in rows]),
        'phase_seconds': {k: distribution(v) for k, v in sorted(phase_values.items())},
        'phase_timing_caveat': 'Completed spans only; nested spans overlap and must not be summed.',
        'top_errors_by_category': {
            category: [{'count': n, 'pattern': pattern} for pattern, n in Counter(
                message_pattern(x.get('error') or x.get('lean-error')) for _, _, x in items).most_common(5)]
            for category, items in by_category.items() if category not in ('proved',)},
        'slowest': [{'theorem': x.get('theorem'), 'walltime': round(data['walltime'], 3), 'outcome': x.get('outcome')}
                    for _, data, x in sorted(rows, key=lambda r: -r[1]['walltime'])[:40]],
    }
    save_json(target / 'summary.json', summary)
    with (target / 'failures.jsonl').open('w') as stream:
        for job, data, x in sorted(rows, key=lambda r: r[0]['input']):
            if x.get('status', data['status']) == 'true' or x.get('outcome') == 'excluded':
                continue
            failure = {**x, 'idx': data['idx'], 'input': job['input'],
                       'log': data.get('log_storage') or f'logs/{data["idx"]:06d}.log',
                       'returncode': data.get('returncode'), 'termination-reason-runner': data.get('reason'),
                       'runner-error': data.get('error'), 'walltime': data['walltime'],
                       'cputime': data.get('cputime'), 'memory': data.get('memory')}
            stream.write(json.dumps(failure, ensure_ascii=False) + '\n')
    keys = ['theorem', 'status'] + [c for c in HAMMER_COLUMNS if c not in ('theorem',)]
    with (target / 'cases.tsv').open('w') as stream:
        stream.write('\t'.join(['idx', *keys, 'walltime', 'cputime', 'memory', 'input']) + '\n')
        for job, data, x in sorted(rows, key=lambda r: r[0]['input']):
            values = [x.get(k, data['status'] if k == 'status' else None) for k in keys]
            values += [round(data['walltime'], 3), data.get('cputime'), data.get('memory'), job['input']]
            stream.write('\t'.join('' if v is None else str(v).replace('\t', ' ') for v in [data['idx'], *values]) + '\n')
    for category, items in by_category.items():
        # Theorem lists can be passed to slurmrunner/make-suite.py --names for a focused rerun.
        names = sorted({x.get('theorem') for _, _, x in items if x.get('theorem')})
        (target / 'categories' / f'{re.sub(r"[^A-Za-z0-9_.-]+", "_", str(category))}.txt').write_text(
            ''.join(name + '\n' for name in names))
    print(f'\n## {runset}: {len(proved)}/{eligible} eligible runs proved ({100 * summary["proved_rate"]:.2f}%), '
          f'by route {summary["proved_by_route"]}')
    if excluded:
        print(f'  excluded from the denominator: {excluded}')
    for category, count in summary['category'].items():
        print(f'  {count:8d}  {category}')
    print(f'  details: {target}')


def save_json(path, obj):
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False))
    tmp.replace(path)


def submit_repeated(ns, submit_once):
    """Separate arrays chained so each full repetition finishes before the next."""
    if ns.repetitions < 1:
        raise ValueError('--repetitions must be positive')
    xml = Path(ns.benchmark).resolve()
    parse_xml(xml)  # Validate before creating the parent manifest.
    out = Path(ns.output).resolve() if ns.output else xml.parent / 'slurm-results' / xml.stem
    out.mkdir(parents=True, exist_ok=True)
    manifest = out / 'manifest.json'
    paths = [out / f'repetition-{i:03d}' / 'manifest.json' for i in range(1, ns.repetitions + 1)]
    if manifest.exists() or any(path.parent.exists() for path in paths):
        raise ValueError(f'Output already contains a run: {out} (choose --output NEW_DIRECTORY)')
    cfg = {'output': str(out), 'xml': str(xml), 'repetition_manifests': list(map(str, paths))}
    save_json(manifest, cfg)
    previous_ids = []
    for i, path in enumerate(paths, 1):
        child = argparse.Namespace(**vars(ns))
        child.repetitions = 1
        child.output = str(path.parent)
        child.sbatch_arg = list(ns.sbatch_arg or [])
        if previous_ids:
            # afterany waits for the entire preceding array, including failures.
            # Failed runs remain visible to collection/resumption.
            dependencies = [arg.split('=', 1)[1] for arg in child.sbatch_arg
                            if arg.startswith('--dependency=')]
            child.sbatch_arg = [arg for arg in child.sbatch_arg if not arg.startswith('--dependency=')]
            dependencies.append('afterany:' + ':'.join(previous_ids))
            child.sbatch_arg.append('--dependency=' + ','.join(dependencies))
        if submit_once is submit_packed:
            child.job_array = True
        print(f'Submitting repetition {i}/{ns.repetitions}...', flush=True)
        submit_once(child)
        saved = json.loads(path.read_text())
        previous_ids = [job_id.split(';', 1)[0] for job_id in
                        saved.get('job_ids', [saved.get('job_id')])]
    print(f'Collect all repetitions: python {shlex.quote(str(Path(__file__).resolve()))} '
          f'collect {shlex.quote(str(manifest))}')


def repeat_statistics(values):
    values = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    if not values:
        return {'count': 0}
    return {'count': len(values), 'mean': statistics.mean(values),
            'stdev': statistics.stdev(values) if len(values) > 1 else None,
            'median': statistics.median(values), 'min': min(values), 'max': max(values)}


def collect_repeated(ns, cfg):
    out = Path(cfg['output'])
    configs, results = [], []
    for path in cfg['repetition_manifests']:
        if not Path(path).exists():
            if not ns.partial:
                raise ValueError(f'Repetition was not submitted: {path}; use --partial')
            configs.append(None)
            results.append({})
            continue
        child = json.loads(Path(path).read_text())
        configs.append(child)
        collect(argparse.Namespace(manifest=path, partial=ns.partial))
        records = read_results(Path(child['output']), bulk_only=child.get('result_format') == 'bulk')
        results.append({idx: records[idx] for idx in usable_results(records)})
    available = [child for child in configs if child is not None]
    if not available:
        raise ValueError('No repetitions have been submitted')
    jobs = available[0]['jobs']
    if any(child['jobs'] != jobs for child in available):
        raise ValueError('Repetitions have different benchmark jobs; cannot aggregate')
    if not ns.partial and any(len(rows) != len(jobs) for rows in results):
        raise ValueError('Repetitions contain unusable results; resume missing work or use --partial')
    case_rows, groups = [], defaultdict(list)
    for idx, job in enumerate(jobs):
        samples = [rows.get(idx) for rows in results]
        measured = [row for row in samples if row is not None]
        eligible = len(measured) == len(results) and all(
            row.get('extracted', {}).get('outcome') != 'excluded' for row in measured)
        row = {'idx': idx, 'runset': job['runset'], 'input': job['input'],
               'theorem': job.get('theorem'), 'completed': len(measured),
               'common_eligible': eligible,
               'statuses': [sample['status'] if sample else None for sample in samples],
               'proved': sum(sample['status'] == 'true' for sample in measured), 'metrics': {}}
        for metric in ('walltime', 'cputime', 'memory', 'hammer-seconds', 'startup-seconds',
                       'replay-seconds', 'vampire-seconds'):
            values = [sample.get(metric) if metric in ('walltime', 'cputime', 'memory')
                      else sample.get('extracted', {}).get(metric) for sample in measured]
            row['metrics'][metric] = repeat_statistics(values)
        case_rows.append(row)
        groups[job['runset']].append((idx, eligible))
    summary = {'repetitions': len(results), 'expected_per_repetition': len(jobs),
               'completed_per_repetition': [len(rows) for rows in results],
               'complete': all(len(rows) == len(jobs) for rows in results),
               'stdev_kind': 'sample', 'runsets': {}}
    for runset, cases in groups.items():
        common = [idx for idx, eligible in cases if eligible]
        proved = [sum(rows[idx]['status'] == 'true' for idx in common) for rows in results]
        totals = {}
        for metric in ('walltime', 'hammer-seconds', 'startup-seconds', 'replay-seconds', 'vampire-seconds'):
            values = []
            for rows in results:
                measured = [rows[idx].get('walltime') if metric == 'walltime'
                            else rows[idx].get('extracted', {}).get(metric) for idx in common]
                values.append(sum(map(float, measured)) if measured and all(v is not None for v in measured) else None)
            totals[metric] = {'per_repetition': values, 'statistics': repeat_statistics(values)}
        summary['runsets'][runset] = {
            'expected_cases': len(cases), 'common_eligible_cases': len(common),
            'completed_per_repetition': [sum(idx in rows for idx, _ in cases) for rows in results],
            'proved_per_repetition': proved, 'proved': repeat_statistics(proved),
            'total_seconds': totals,
            'proved_rate': repeat_statistics([n / len(common) for n in proved]) if common else {'count': 0},
            'proved_every_repetition': sum(all(rows[idx]['status'] == 'true' for rows in results) for idx in common),
            'proved_any_repetition': sum(any(rows[idx]['status'] == 'true' for rows in results) for idx in common)}
    save_json(out / 'repetition-summary.json', summary)
    with (out / 'repetition-cases.jsonl').open('w') as stream:
        for row in case_rows:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')
    print(f'Repetition statistics: {out / "repetition-summary.json"}')


def submit(ns):
    if getattr(ns, 'repetitions', 1) != 1:
        return submit_repeated(ns, submit)
    xml = Path(ns.benchmark).resolve()
    cfg = parse_xml(xml)
    out = Path(ns.output).resolve() if ns.output else (xml.parent / 'slurm-results' / xml.stem).resolve()
    out.mkdir(parents=True, exist_ok=True)
    (out / 'logs').mkdir(exist_ok=True)
    (out / 'slurm').mkdir(exist_ok=True)
    manifest = out / 'manifest.json'
    if manifest.exists():
        raise ValueError(f'Output already contains a run: {out} (choose --output NEW_DIRECTORY)')
    cfg.update({'output': str(out), 'xml': str(xml), 'created': dt.datetime.now(dt.timezone.utc).isoformat(),
                'partition': ns.partition, 'extra_sbatch': ns.sbatch_arg or []})
    save_json(manifest, cfg)
    script = Path(__file__).resolve()
    # Slurm memory and time are per array task. One task is one benchmark execution.
    slack = max(2, int(ns.slack))
    cmd = ['sbatch', '--parsable', f'--partition={ns.partition}', '--nodes=1', '--ntasks=1',
           f'--cpus-per-task={cfg["cpus"]}', f'--mem={cfg["mem_mb"]}M',
           f'--time={int((cfg["seconds"] + slack * 60 + 59) // 60)}',
           f'--array=0-{len(cfg["jobs"])-1}%{ns.parallel}',
           f'--output={out}/slurm/%A_%a.out', f'--error={out}/slurm/%A_%a.err',
           '--job-name=slurmbench']
    for arg in ns.sbatch_arg or []:
        if not arg.startswith('--'):
            raise ValueError('--sbatch-arg must start with --')
        cmd.append(arg)
    cmd += [str(script), 'worker', str(manifest)]
    try:
        job_id = subprocess.check_output(cmd, text=True).strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        print('sbatch failed; manifest saved at', manifest, file=sys.stderr)
        raise
    cfg['job_id'] = job_id
    save_json(manifest, cfg)
    print(f'Submitted {len(cfg["jobs"])} runs as Slurm array job {job_id}')
    print(f'After completion: python {shlex.quote(str(script))} collect {shlex.quote(str(manifest))}')


class ResultJournal:
    """One writer per node/attempt; publish the result after its log is flushed.

    Separate attempt files avoid cross-node locking on shared filesystems.
    A newline commits each record; interrupted trailing records are ignored.
    """
    def __init__(self, output, prefix):
        self.output = Path(output)
        directory = self.output / 'journals'
        directory.mkdir(exist_ok=True)
        stem = f'{prefix}-{uuid.uuid4().hex}'
        self.log_path = directory / f'{stem}.logs'
        self.records_path = directory / f'{stem}.jsonl'
        self.log = self.log_path.open('wb')
        self.records = self.records_path.open('wb')
        self.lock = threading.Lock()

    def save(self, data, logfile, source_offset=0, source_length=None, source_encoding=None):
        with self.lock:
            offset = self.log.tell()
            destination = self.log if source_encoding == 'gzip' else gzip.GzipFile(
                filename='', mode='wb', fileobj=self.log, compresslevel=1, mtime=0)
            try:
                with Path(logfile).open('rb') as stream:
                    stream.seek(source_offset)
                    if source_length is None:
                        shutil.copyfileobj(stream, destination)
                    else:
                        remaining = source_length
                        while remaining:
                            chunk = stream.read(min(remaining, 1024 * 1024))
                            if not chunk:
                                raise ValueError(f'Truncated log in {logfile}')
                            destination.write(chunk)
                            remaining -= len(chunk)
            finally:
                if destination is not self.log:
                    destination.close()
            length = self.log.tell() - offset
            self.log.flush()
            data['log_storage'] = {'path': str(self.log_path.relative_to(self.output)),
                                   'offset': offset, 'length': length, 'encoding': 'gzip'}
            data['completed_at_ns'] = time.time_ns()
            self.records.write((json.dumps(data, separators=(',', ':')) + '\n').encode())
            self.records.flush()

    def close(self):
        self.records.close()
        self.log.close()


def read_results(output, bulk_only=False, wanted=None, prefix=None, progress=None):
    """Read journals sequentially; retain the latest complete result per index."""
    output = Path(output)
    results = {}
    stamps = {}
    last_update = time.monotonic()

    def report():
        nonlocal last_update
        if progress is not None and time.monotonic() - last_update >= 5:
            progress(len(results))
            last_update = time.monotonic()

    journals = output / 'journals'
    if journals.is_dir():
        for entry in os.scandir(journals):
            if not entry.name.endswith('.jsonl') or (prefix and not entry.name.startswith(prefix)):
                continue
            with open(entry.path, 'rb') as stream:
                for line in stream:
                    if not line.endswith(b'\n'):
                        break
                    data = json.loads(line)
                    idx = data['idx']
                    if wanted is not None and idx not in wanted:
                        continue
                    stamp = data.get('completed_at_ns', 0)
                    if stamp >= stamps.get(idx, -1):
                        results[idx], stamps[idx] = data, stamp
                    report()
    logs = output / 'logs'
    if not bulk_only and logs.is_dir():
        # Enumerate existing files once, rather than stat every expected run.
        for entry in os.scandir(logs):
            if not re.fullmatch(r'\d+\.json', entry.name):
                continue
            idx = int(entry.name[:-5])
            if idx in results or (wanted is not None and idx not in wanted):
                continue
            with open(entry.path) as stream:
                results[idx] = json.load(stream)
            report()
    if progress is not None:
        progress(len(results))
    return results


def read_run_log(output, data):
    location = data.get('log_storage')
    if location:
        with (Path(output) / location['path']).open('rb') as stream:
            stream.seek(location['offset'])
            contents = stream.read(location['length'])
            if location.get('encoding') == 'gzip':
                contents = gzip.decompress(contents)
            return contents.decode(errors='replace')
    return (Path(output) / 'logs' / f'{data["idx"]:06d}.log').read_text(errors='replace')


def show_log(ns):
    cfg = json.loads(Path(ns.manifest).read_text())
    results = read_results(cfg['output'], bulk_only=cfg.get('result_format') == 'bulk', wanted={ns.idx})
    if ns.idx not in results:
        raise ValueError(f'No completed result for index {ns.idx}')
    print(read_run_log(cfg['output'], results[ns.idx]), end='')


def pack_results(ns):
    """Consolidate an already completed legacy run without deleting originals."""
    manifest = Path(ns.manifest)
    cfg = json.loads(manifest.read_text())
    output = Path(cfg['output'])
    results = read_results(output, bulk_only=cfg.get('result_format') == 'bulk')
    missing = [idx for idx in range(len(cfg['jobs'])) if idx not in results]
    if missing:
        raise ValueError(f'{len(missing)} runs are incomplete; pack-results requires a completed run')
    if cfg.get('result_format') == 'bulk':
        print('Results already use bulk storage.')
        return
    journal = ResultJournal(output, 'packed-legacy')
    try:
        for idx in range(len(cfg['jobs'])):
            data = dict(results[idx])
            location = data.get('log_storage')
            if location:
                journal.save(data, output / location['path'], location['offset'], location['length'], location.get('encoding'))
            else:
                journal.save(data, output / 'logs' / f'{idx:06d}.log')
    finally:
        journal.close()
    cfg['result_format'] = 'bulk'
    cfg['packed_legacy_results'] = True
    save_json(manifest, cfg)
    print(f'Packed {len(cfg["jobs"])} results. Originals retained; collection now reads journals only.')


def run_one(cfg, idx, core=None, journal=None, local_log=None):
    job = cfg['jobs'][idx]
    output = Path(cfg['output'])
    logfile = Path(local_log) if local_log is not None else output / 'logs' / f'{idx:06d}.log'
    resultfile = output / 'logs' / f'{idx:06d}.json'
    command = [cfg['executable'], *job['args']]
    kind = tool_kind(cfg)
    if kind == 'vampire':
        # Match the BenchExec Vampire tool-info module's default limits.
        argv = command[1:-1]
        extra = []
        if '-t' not in argv and '--time_limit' not in argv:
            extra += ['-t', f'{cfg["seconds"]:g}s']
        if '-m' not in argv and '--memory_limit' not in argv:
            extra += ['-m', str(int(cfg['mem_mb'] * 1000000 / (1024 * 1024)))]
        command = [command[0], *argv, *extra, command[-1]]
    if core is not None:
        command = ['taskset', '-c', str(core), *command]
    t0 = time.monotonic()
    reason = ''
    code = None
    error = None
    cpu = None
    rss = None
    try:
        with logfile.open('wb') as log:
            proc = subprocess.Popen(command, cwd=cfg['workdir'], stdout=log, stderr=subprocess.STDOUT,
                                    start_new_session=True,
                                    env={**os.environ, **cfg['env_overrides']} if cfg.get('env_overrides') else None)
            # wait4(pid) gives resource usage for exactly this child, unlike
            # RUSAGE_CHILDREN (shared by all Python worker threads).
            deadline = t0 + cfg['seconds']
            while True:
                pid, state, ru = os.wait4(proc.pid, os.WNOHANG)
                if pid:
                    code = os.waitstatus_to_exitcode(state)
                    proc.returncode = code
                    cpu = ru.ru_utime + ru.ru_stime
                    rss = int(ru.ru_maxrss) * 1024  # Linux returns KiB
                    break
                if time.monotonic() >= deadline:
                    reason = 'TIMEOUT'
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    _, state, ru = os.wait4(proc.pid, 0)
                    code = os.waitstatus_to_exitcode(state)
                    proc.returncode = code
                    cpu = ru.ru_utime + ru.ru_stime
                    rss = int(ru.ru_maxrss) * 1024
                    break
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
    except Exception as ex:
        reason = 'ERROR'
        error = repr(ex)
        with logfile.open('ab') as log:
            log.write((f'\nslurmbench error: {error}\n').encode())
    elapsed = time.monotonic() - t0
    text = logfile.read_text(errors='replace')
    m = re.search(cfg['status_regex'], text, re.M) if cfg['status_regex'] else None
    status = ('TIMEOUT' if reason == 'TIMEOUT' else 'ERROR' if reason else
              m.group(1) if m and m.lastindex else
              m.group(0) if m else 'DONE' if code == 0 else 'ERROR')
    extracted = {}
    if kind == 'vampire':
        extracted = vampire_metrics(text, code, timed_out=(reason == 'TIMEOUT'))
    elif kind == 'hammer' and reason != 'ERROR':
        extracted = hammer_metrics(text, code, timed_out=(reason == 'TIMEOUT'), prover=job_prover(job))
    if extracted:
        status = extracted['status']
    data = {'idx': idx, 'status': status, 'returncode': code, 'walltime': elapsed,
                           'cputime': cpu, 'memory': rss, 'reason': reason, 'error': error,
                           'hostname': os.uname().nodename, 'extracted': extracted}
    if journal is None:
        save_json(resultfile, data)
    else:
        journal.save(data, logfile)


def worker(ns):
    cfg = json.loads(Path(ns.manifest).resolve().read_text())
    run_one(cfg, int(os.environ['SLURM_ARRAY_TASK_ID']))


def stage_hammer(cfg, scratch):
    """Return a node-local run configuration and its temporary staging directory."""
    if tool_kind(cfg) != 'hammer':
        raise ValueError('--scratch-root currently supports hammer suites only')
    if not shutil.which('rsync'):
        raise ValueError('--scratch-root requires rsync on each compute node')
    configs = {}
    for job in cfg['jobs']:
        args = job['args']
        if '--config' not in args:
            raise ValueError('--scratch-root requires --config in each hammer command')
        filename = args[args.index('--config') + 1]
        path = (Path(cfg['workdir']) / filename).resolve()
        configs[filename] = json.loads(path.read_text())
    root = Path(tempfile.mkdtemp(prefix='slurmbench-', dir=scratch))
    try:
        mappings = {}

        def copy_tree(source):
            source = Path(source).resolve()
            if not source.is_dir():
                raise ValueError(f'Staging source directory does not exist: {source}')
            for old, new in mappings.items():
                if source.is_relative_to(old):
                    return new / source.relative_to(old)
            target = root / f'tree-{len(mappings)}'
            target.mkdir()
            print(f'staging {source} -> {target}', flush=True)
            # Dereference dependency symlinks so reads cannot fall back to the
            # shared filesystem. No build runs on the compute node.
            subprocess.run(['rsync', '-aL', '--exclude=.git/', '--exclude=__pycache__/',
                            '--exclude=/artifacts/', f'{source}/', f'{target}/'], check=True)
            mappings[source] = target
            return target

        def relocate(path):
            path = Path(path).resolve()
            for old, new in mappings.items():
                if path.is_relative_to(old):
                    return str(new / path.relative_to(old))
            raise ValueError(f'Path is outside staged directories: {path}')

        # Copy the runner checkout too if --project selected a separate build.
        executable = Path(cfg['executable']).resolve()
        for config in configs.values():
            if not config.get('lean'):
                raise ValueError('Regenerate the suite: staging requires a recorded Lean binary')
            copy_tree(config['project'])
            copy_tree(Path(config['lean']).resolve().parent.parent)
        copy_tree(executable.parent.parent)
        for config in configs.values():
            project = Path(config['project'])
            for key in ('LEAN_PATH', 'LEAN_SRC_PATH', 'LD_LIBRARY_PATH', 'DYLD_LIBRARY_PATH'):
                for part in config.get('lean_env', {}).get(key, '').split(os.pathsep):
                    if part:
                        search_path = (project / part).resolve()
                        if search_path.exists():
                            copy_tree(search_path)
                        elif not any(search_path.is_relative_to(old) for old in mappings):
                            # Lake can include unbuilt dependency directories.
                            # Keep these absent, including external search paths,
                            # but relocate them so reads remain node-local.
                            mappings[search_path] = root / f'missing-search-path-{len(mappings)}'
            for key in ('vampire', 'tptp_parser_dynlib'):
                if config.get(key):
                    copy_tree(Path(config[key]).resolve().parent)
            # Native libraries (`--plugin=`/`--load-dynlib=`) of the
            # precompiled hammer and lean-auto; usually inside the trees above.
            for flag in config.get('lean_args', []):
                option, _, library = flag.partition('=')
                if library:
                    copy_tree(Path(library).resolve().parent)

        local_configs = {}
        for index, (filename, config) in enumerate(configs.items()):
            local = dict(config)
            for key in ('project', 'lean', 'vampire', 'tptp_parser_dynlib'):
                if local.get(key):
                    local[key] = relocate(local[key])
            local['lean_env'] = dict(config.get('lean_env', {}))
            for key in ('LEAN_PATH', 'LEAN_SRC_PATH', 'LD_LIBRARY_PATH', 'DYLD_LIBRARY_PATH'):
                if key in local['lean_env']:
                    local['lean_env'][key] = os.pathsep.join(
                        relocate(Path(config['project']) / part) if part else ''
                        for part in local['lean_env'][key].split(os.pathsep))
            if 'LEAN_SYSROOT' in local['lean_env']:
                local['lean_env']['LEAN_SYSROOT'] = relocate(local['lean_env']['LEAN_SYSROOT'])
            local['lean_args'] = []
            for flag in config.get('lean_args', []):
                option, separator, library = flag.partition('=')
                local['lean_args'].append(option + separator + relocate(library) if library else flag)
            destination = root / f'suite-config-{index}.json'
            save_json(destination, local)
            local_configs[filename] = str(destination)
        staged = dict(cfg)
        staged['executable'] = relocate(executable)
        staged['jobs'] = []
        for job in cfg['jobs']:
            local_job = dict(job)
            args = list(job['args'])
            index = args.index('--config') + 1
            args[index] = local_configs[args[index]]
            local_job['args'] = args
            staged['jobs'].append(local_job)
        (root / 'tmp').mkdir()
        staged['env_overrides'] = {**cfg.get('env_overrides', {}), 'TMPDIR': str(root / 'tmp')}
        return staged, root
    except BaseException:
        shutil.rmtree(root)
        raise


def batch_key(job):
    """Jobs with equal keys differ only in their theorem: same module, prover
    and suite options. Only compact jobs with defining-module metadata batch."""
    args = job['args']
    if not job.get('theorem') or '--module' not in args:
        return None
    index = args.index('--theorem')
    return tuple(args[:index] + args[index + 2:])


def batches(cfg, limit):
    """Consecutive manifest jobs with one batch key, in chunks of `limit`."""
    groups, current, key = [], [], None
    for idx, job in enumerate(cfg['jobs']):
        job_key = batch_key(job)
        if current and (job_key is None or job_key != key or len(current) >= limit):
            groups.append(current)
            current = []
        current.append(idx)
        key = job_key
    if current:
        groups.append(current)
    return groups


def split_sections(text):
    """The log of a batched run, split at each theorem's header line. Lines
    before the first header (the shared Lean output) go to the first section.

    Lean emits `[vampire.event]` records while it replays the batch, before
    `run-hammer-task` prints the individual result headers.  Copy each named
    event to its theorem section so every result retains its own diagnostics.
    """
    sections, name, current = {}, None, []
    events = defaultdict(list)
    for line in text.splitlines(keepends=True):
        if line.startswith('[vampire.event] '):
            try:
                event = json.loads(line[len('[vampire.event] '):])
            except json.JSONDecodeError:
                event = None
            if isinstance(event, dict) and isinstance(event.get('name'), str):
                events[event['name']].append(line)
        if line.startswith(HAMMER_THEOREM):
            if name is not None:
                sections[name] = ''.join(current)
                current = []
            name = line[len(HAMMER_THEOREM):].split(';')[0]
        current.append(line)
    if name is not None:
        sections[name] = ''.join(current)
    for name, section in sections.items():
        # The first section already owns shared pre-header output.  Removing
        # event lines first avoids duplicate diagnostics while retaining all
        # non-event Lean output and malformed lines for debugging.
        retained = ''.join(line for line in section.splitlines(keepends=True)
                           if not line.startswith('[vampire.event] '))
        sections[name] = ''.join(events.get(name, ())) + retained
    return sections


def run_batch(cfg, idxs, core=None, journal=None, local_log=None):
    """Run several theorems of one module in one `run-hammer-task` process
    (one source replay), and record each as its own job result."""
    if len(idxs) == 1:
        return run_one(cfg, idxs[0], core=core, journal=journal, local_log=local_log)
    jobs = [cfg['jobs'][idx] for idx in idxs]
    output = Path(cfg['output'])
    logfile = Path(local_log) if local_log is not None else output / 'logs' / f'batch-{idxs[0]:06d}.log'
    first = jobs[0]['args']
    index = first.index('--theorem')
    command = [cfg['executable'], *first[:index], *first[index + 2:]]
    for job in jobs:
        command += ['--theorem', job['theorem']]
    if core is not None:
        command = ['taskset', '-c', str(core), *command]
    t0 = time.monotonic()
    reason, code, error, cpu, rss = '', None, None, None, None
    try:
        with logfile.open('wb') as log:
            proc = subprocess.Popen(command, cwd=cfg['workdir'], stdout=log, stderr=subprocess.STDOUT,
                                    start_new_session=True,
                                    env={**os.environ, **cfg['env_overrides']} if cfg.get('env_overrides') else None)
            # Each theorem keeps the per-run limit.
            deadline = t0 + cfg['seconds'] * len(jobs)
            while True:
                pid, state, ru = os.wait4(proc.pid, os.WNOHANG)
                if pid:
                    code = os.waitstatus_to_exitcode(state)
                    cpu, rss = ru.ru_utime + ru.ru_stime, int(ru.ru_maxrss) * 1024
                    break
                if time.monotonic() >= deadline:
                    reason = 'TIMEOUT'
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    _, state, ru = os.wait4(proc.pid, 0)
                    code = os.waitstatus_to_exitcode(state)
                    cpu, rss = ru.ru_utime + ru.ru_stime, int(ru.ru_maxrss) * 1024
                    break
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
    except Exception as ex:
        reason, error = 'ERROR', repr(ex)
        with logfile.open('ab') as log:
            log.write((f'\nslurmbench error: {error}\n').encode())
    elapsed = time.monotonic() - t0
    sections = split_sections(logfile.read_text(errors='replace'))
    for idx, job in zip(idxs, jobs):
        section = sections.get(job['theorem'], '')
        extracted = {}
        if reason != 'ERROR':
            extracted = hammer_metrics(section, code, timed_out=(reason == 'TIMEOUT' and not section),
                                       prover=job_prover(job))
        status = extracted.get('status') or ('TIMEOUT' if reason == 'TIMEOUT' else 'ERROR')
        # Process resources are shared by the batch; wall time is the
        # theorem's own where its result records it.
        data = {'idx': idx, 'status': status, 'returncode': code,
                'walltime': extracted.get('hammer-seconds') or elapsed / len(jobs),
                'cputime': cpu, 'memory': rss, 'reason': reason, 'error': error,
                'hostname': os.uname().nodename, 'extracted': extracted,
                'batch': {'size': len(jobs), 'walltime': elapsed, 'first': idxs[0]}}
        section_file = logfile.with_name(logfile.name + f'.{idx}')
        section_file.write_text(section)
        if journal is None:
            save_json(output / 'logs' / f'{idx:06d}.json', data)
            section_file.replace(output / 'logs' / f'{idx:06d}.log')
        else:
            journal.save(data, section_file)
            section_file.unlink()


def physical_core_selection(allowed, slots, sysroot=Path('/sys/devices/system/cpu')):
    """Select one permitted logical CPU per physical core; fail if unavailable."""
    selected, seen = [], set()
    for cpu in sorted(allowed):
        root = sysroot / f'cpu{cpu}/topology'
        try:
            key = (int((root / 'physical_package_id').read_text()), int((root / 'core_id').read_text()))
        except (OSError, ValueError) as error:
            raise ValueError(f'Cannot verify physical core for CPU {cpu}: {error}') from error
        if key not in seen:
            seen.add(key)
            selected.append(cpu)
    if len(selected) < slots:
        raise ValueError(f'Only {len(selected)} physical cores available for {slots} slots')
    return selected[:slots]


def read_cpu_controls(sysroot=Path('/sys/devices/system/cpu')):
    controls = {}
    for name, relative in (('boost', 'cpufreq/boost'), ('smt', 'smt/control')):
        path = sysroot / relative
        item = {'path': str(path), 'value': None}
        try:
            item['value'] = path.read_text().strip()
        except OSError as error:
            item['read_error'] = str(error)
        controls[name] = item
    return controls


def cpu_control_errors(controls):
    errors = []
    if controls['boost']['value'] != '0':
        errors.append(f"{controls['boost']['path']} must read 0; got {controls['boost']['value']!r}")
    if controls['smt']['value'] not in {'off', 'forceoff', 'notsupported'}:
        errors.append(f"{controls['smt']['path']} must read off, forceoff or notsupported; got {controls['smt']['value']!r}")
    return errors


def record_node_environment(cfg, shard, rank, cores, controls=None):
    controls = read_cpu_controls() if controls is None else controls
    topology = []
    for cpu in cores:
        root = Path(f'/sys/devices/system/cpu/cpu{cpu}/topology')
        values = {'cpu': cpu}
        for name in ('physical_package_id', 'core_id', 'thread_siblings_list'):
            try:
                values[name] = (root / name).read_text().strip()
            except OSError:
                values[name] = None
        topology.append(values)
    data = {'recorded_at': dt.datetime.now(dt.timezone.utc).isoformat(),
            'hostname': os.uname().nodename, 'kernel': os.uname().release,
            'selected_cpus': cores, 'affinity': sorted(os.sched_getaffinity(0)), 'topology': topology,
            'physical_core_selection': bool(cfg.get('physical_cores')),
            'cpu_controls': controls,
            'cpu_control_check': {'required': bool(cfg.get('require_no_boost_no_smt')),
                                  'errors': cpu_control_errors(controls)},
            'slurm': {key: value for key, value in os.environ.items() if key.startswith('SLURM_')}}
    for name in ('cpuinfo', 'meminfo'):
        path = Path('/proc') / name
        if path.exists():
            data[name] = path.read_text()
    directory = Path(cfg['output']) / 'node-environments'
    directory.mkdir(parents=True, exist_ok=True)
    # Distinct records for resumed attempts as well as the original workers.
    save_json(directory / f'shard-{shard:03d}-rank-{rank:03d}-{uuid.uuid4().hex}.json', data)


def packed_worker(ns):
    """One srun task per node; one benchmark per allocated logical CPU."""
    cfg = json.loads(Path(ns.manifest).resolve().read_text())
    rank = int(os.environ['SLURM_PROCID'])
    shard = getattr(ns, 'shard', 0)
    shards = int(cfg.get('shards', 1))
    if not 0 <= shard < shards:
        raise ValueError(f'Invalid shard {shard} for {shards} total shards')
    nodes = int(cfg['packed_nodes'])
    slots = int(cfg['packed_cpus_per_node'])
    cores = sorted(os.sched_getaffinity(0))
    if not shutil.which('taskset'):
        raise ValueError('taskset is required for packed mode')
    if len(cores) < slots:
        raise ValueError(f'Node rank {rank} has only {len(cores)} CPUs in affinity, expected {slots}. '
                         'Check Slurm CPU binding/allocation.')
    if cfg.get('physical_cores'):
        cores = physical_core_selection(cores, slots)
    controls = read_cpu_controls()
    record_node_environment(cfg, shard, rank, cores[:slots], controls)
    if cfg.get('require_no_boost_no_smt') and cpu_control_errors(controls):
        raise ValueError('CPU-control preflight failed: ' + '; '.join(cpu_control_errors(controls)))
    # Static distribution: each node handles every Nth benchmark, or every Nth
    # batch of theorems from one module.
    limit = int(cfg.get('batch_by_module') or 1)
    groups = batches(cfg, limit) if limit > 1 else [[idx] for idx in range(len(cfg['jobs']))]
    groups = groups[shard * nodes + rank::shards * nodes]
    indices = [idx for group in groups for idx in group]
    journal_prefix = f'shard-{shard:03d}-rank-{rank:03d}-'
    if getattr(ns, 'resume', False) or cfg.get('resume'):
        # Skip runs that already have a result (e.g. after resubmitting a
        # cancelled shard). A batch killed at the deadline has no usable
        # result, so it runs again.
        done = read_results(cfg['output'], bulk_only=cfg.get('result_format') == 'bulk',
                            wanted=set(indices), prefix=None if cfg.get('packed_legacy_results') else journal_prefix)
        done = usable_results(done)
        indices = [i for i in indices if i not in done]
        groups = [[i for i in group if i not in done] for group in groups]
        groups = [group for group in groups if group]
    print(f'node={os.uname().nodename} shard={shard}/{shards} rank={rank} cores={cores[:slots]} runs={len(indices)}', flush=True)
    if not indices:
        return
    staging = None
    journal = None
    local_logs = None
    try:
        if cfg.get('scratch_root'):
            cfg, staging = stage_hammer(cfg, cfg['scratch_root'])
        if cfg.get('result_format') == 'bulk':
            journal = ResultJournal(cfg['output'], journal_prefix.rstrip('-'))
            local_logs = tempfile.TemporaryDirectory(prefix='slurmbench-logs-', dir=staging)
        # Stop all slot queues if controls change before a later batch starts.
        stop_slots = threading.Event()
        # Worker threads each execute a serial queue pinned to a distinct CPU.
        def slot_loop(slot):
            for group in groups[slot::slots]:
                if stop_slots.is_set():
                    return
                if cfg.get('require_no_boost_no_smt'):
                    controls = read_cpu_controls()
                    errors = cpu_control_errors(controls)
                    if errors:
                        stop_slots.set()
                        record_node_environment(cfg, shard, rank, cores[:slots], controls)
                        raise ValueError('CPU-control check failed before batch: ' + '; '.join(errors))
                run_batch(cfg, group, core=cores[slot], journal=journal,
                          local_log=Path(local_logs.name) / f'slot-{slot}.log' if local_logs else None)
        with ThreadPoolExecutor(max_workers=slots) as pool:
            list(pool.map(slot_loop, range(slots)))
    finally:
        if journal is not None:
            journal.close()
        if local_logs is not None:
            local_logs.cleanup()
        if staging is not None:
            shutil.rmtree(staging)


def submit_packed(ns):
    if getattr(ns, 'repetitions', 1) != 1:
        return submit_repeated(ns, submit_packed)
    xml = Path(ns.benchmark).resolve()
    cfg = parse_xml(xml)
    if cfg['cpus'] != 1:
        raise ValueError('Packed mode requires cpuCores="1"; each run is pinned to one CPU')
    if ns.scratch_root and tool_kind(cfg) != 'hammer':
        raise ValueError('--scratch-root currently supports hammer suites only')
    if ns.nodes < 1 or ns.cpus_per_node < 1 or ns.shards < 1:
        raise ValueError('--nodes, --cpus-per-node and --shards must be positive')
    if ns.array_parallel is not None and (not ns.job_array or ns.array_parallel < 1):
        raise ValueError('--array-parallel requires --job-array and a positive limit')
    out = Path(ns.output).resolve() if ns.output else (xml.parent / 'slurm-results' / xml.stem).resolve()
    out.mkdir(parents=True, exist_ok=True)
    (out / 'logs').mkdir(exist_ok=True)
    (out / 'slurm').mkdir(exist_ok=True)
    manifest = out / 'manifest.json'
    if manifest.exists():
        raise ValueError(f'Output already contains a run: {out} (choose --output NEW_DIRECTORY)')
    cfg.update({'output': str(out), 'xml': str(xml),
                'created': dt.datetime.now(dt.timezone.utc).isoformat(),
                'partition': ns.partition,
                'packed_nodes': ns.nodes, 'packed_cpus_per_node': ns.cpus_per_node,
                'shards': ns.shards, 'job_ids': [], 'scratch_root': ns.scratch_root,
                'result_format': getattr(ns, 'result_format', 'bulk'),
                'job_array': ns.job_array, 'array_parallel': ns.array_parallel,
                'batch_by_module': getattr(ns, 'batch_by_module', 1),
                'physical_cores': getattr(ns, 'physical_cores', False) or getattr(ns, 'require_no_boost_no_smt', False),
                'require_no_boost_no_smt': getattr(ns, 'require_no_boost_no_smt', False)})
    save_json(manifest, cfg)
    script = Path(__file__).resolve()
    shard_slots = ns.nodes * ns.cpus_per_node * ns.shards
    concurrent_shards = min(ns.shards, ns.array_parallel or ns.shards)
    total_slots = ns.nodes * ns.cpus_per_node * concurrent_shards
    # Static strided assignments can differ by at most one run per slot.
    rounds = math.ceil(len(cfg['jobs']) / shard_slots)
    # With fixed wall limits each slot executes at most `rounds` benchmarks.
    minutes = max(1, math.ceil((rounds * cfg['seconds'] + ns.slack * 60) / 60))
    if ns.time:
        # Typical runs are far below the per-run limit; the worst case may exceed partition limits.
        minutes = ns.time
    # The per-run memlimit times the slots per node, unless given explicitly.
    # Runs on one node share the page cache (e.g. memory-mapped libraries).
    memory = ns.mem or f'{cfg["mem_mb"] * ns.cpus_per_node}M'
    # Shard scripts are also used for selective resubmission of array elements.
    job_ids = []
    for shard in range(ns.shards):
        batchfile = out / 'slurm' / f'launch-{shard:03d}.sh'
        launch = (f'#!/bin/bash\nset -euo pipefail\n'
                  f'srun --ntasks={ns.nodes} --ntasks-per-node=1 --cpu-bind=cores '
                  + ('--kill-on-bad-exit=1 ' if cfg.get('require_no_boost_no_smt') else '') +
                  f'{shlex.quote(sys.executable)} {shlex.quote(str(script))} '
                  f'packed-worker {shlex.quote(str(manifest))} {shard}\n')
        batchfile.write_text(launch)
        batchfile.chmod(0o755)
    if ns.job_array:
        arrayfile = out / 'slurm' / 'launch-array.sh'
        arrayfile.write_text('#!/bin/bash\nset -euo pipefail\n'
                             f'exec bash {shlex.quote(str(out / "slurm"))}/launch-'
                             '"$(printf \'%03d\' "$SLURM_ARRAY_TASK_ID")".sh\n')
        arrayfile.chmod(0o755)
    for shard in range(1 if ns.job_array else ns.shards):
        batchfile = arrayfile if ns.job_array else out / 'slurm' / f'launch-{shard:03d}.sh'
        log_pattern = '%A_%a' if ns.job_array else '%j'
        cmd = ['sbatch', '--parsable', f'--partition={ns.partition}',
               f'--nodes={ns.nodes}', f'--ntasks={ns.nodes}',
               '--ntasks-per-node=1', f'--cpus-per-task={ns.cpus_per_node}',
               f'--mem={memory}',
               f'--time={minutes}', f'--output={out}/slurm/{log_pattern}.out',
               f'--error={out}/slurm/{log_pattern}.err', '--job-name=slurmbench-packed']
        if ns.job_array:
            array = f'0-{ns.shards - 1}'
            if ns.array_parallel:
                array += f'%{ns.array_parallel}'
            cmd.append(f'--array={array}')
        for arg in ns.sbatch_arg or []:
            if not arg.startswith('--'):
                raise ValueError('--sbatch-arg must start with --')
            cmd.append(arg)
        cmd.append(str(batchfile))
        try:
            job_id = subprocess.check_output(cmd, text=True).strip()
        except (FileNotFoundError, subprocess.CalledProcessError):
            cfg['job_ids'] = job_ids
            save_json(manifest, cfg)
            print(f'Failed to submit shard {shard}; already submitted jobs: {job_ids}', file=sys.stderr)
            raise
        job_ids.append(job_id)
        cfg['job_ids'] = job_ids
        for index in range(ns.shards) if ns.job_array else [shard]:
            cfg.setdefault('shard_commands', {})[str(index)] = cmd
        save_json(manifest, cfg)
        label = f'array with {ns.shards} shards' if ns.job_array else f'shard {shard+1}/{ns.shards}'
        print(f'Submitted {label}: job {job_id} ({ns.nodes} nodes per shard, '
              f'{ns.cpus_per_node} CPUs/node)', flush=True)
    print(f'{len(cfg["jobs"])} benchmark executions distributed over {ns.shards} jobs '
          f'({total_slots} maximum concurrent runs).')
    print(f'After completion: python {shlex.quote(str(script))} collect {shlex.quote(str(manifest))}')


def resubmit_shard(ns):
    """Resubmit one packed shard, skipping runs that already have a usable result."""
    manifest = Path(ns.manifest).resolve()
    cfg = json.loads(manifest.read_text())
    batchfile = Path(cfg['output']) / 'slurm' / f'launch-{ns.shard:03d}.sh'
    if not batchfile.exists():
        raise ValueError(f'No packed launch script for shard {ns.shard}: {batchfile}')
    text = batchfile.read_text()
    if '--resume' not in text:
        batchfile.write_text(text.rstrip('\n') + ' --resume\n')
    # The original resources (nodes, CPUs, memory, time); later options override earlier ones.
    original = cfg['shard_commands'][str(ns.shard)]
    selector = [f'--array={ns.shard}'] if cfg.get('job_array') else []
    options = [arg for arg in original[:-1] if not (selector and arg.startswith('--array='))]
    cmd = [*options, *(ns.sbatch_arg or []), *selector, original[-1]]
    job_id = subprocess.check_output(cmd, text=True).strip()
    cfg['job_ids'].append(job_id)
    save_json(manifest, cfg)
    print(f'Resubmitted shard {ns.shard} as job {job_id}; runs with results are skipped.')


def column(parent, title, value):
    ET.SubElement(parent, 'column', {'title': title, 'value': str(value)})


def comparison_diagnostics(out, completed, expected_provers=None):
    """Paired results on the common completed, eligible theorem set."""
    provers = ('vampire-hammer', 'duper', 'aesop', 'simp_all', 'grind')
    groups = defaultdict(dict)
    for job, data, _ in completed:
        if '--prover' not in job['args']:
            continue
        prover = job['args'][job['args'].index('--prover') + 1]
        seconds_arg = job['args'][job['args'].index('--search-seconds') + 1]
        groups[seconds_arg].setdefault(job['input'], {})[prover] = data
    if 'lean-auto-duper' in (expected_provers or ()) or any('lean-auto-duper' in rows for cases in groups.values() for rows in cases.values()):
        provers = ('vampire-hammer', 'lean-auto-duper', *provers[1:])
    if 'lean-smt' in (expected_provers or ()) or any('lean-smt' in rows for cases in groups.values() for rows in cases.values()):
        provers = (*provers, 'lean-smt')
    for budget, cases in groups.items():
        def summarize(selected):
            common = {task: rows for task, rows in cases.items() if all(p in rows for p in selected)}
            eligible = {task: rows for task, rows in common.items() if not any(
                rows[p].get('extracted', {}).get('outcome') == 'excluded' for p in selected)}
            solved = {p: {task for task, rows in eligible.items() if rows[p]['status'] == 'true'} for p in selected}
            report = {'provers': list(selected),
                  'common_completed': len(common), 'common_eligible': len(eligible),
                  'incomplete': len(cases) - len(common), 'excluded': len(common) - len(eligible),
                  'proved': {p: len(solved[p]) for p in selected},
                  'proved_by_any': len(set().union(*solved.values())),
                  'proved_by_all': len(set.intersection(*solved.values())),
                  'only_this_prover': {p: len(solved[p] - set().union(*(solved[q] for q in selected if q != p))) for p in selected},
                  'pairwise': {p: {q: {'both': len(solved[p] & solved[q]),
                                      'only_first': len(solved[p] - solved[q]),
                                      'only_second': len(solved[q] - solved[p])} for q in selected if q != p}
                               for p in selected}}
            return report, common, eligible

        report, common, eligible = summarize(provers)
        report.update(search_budget_seconds=float(budget), theorems_with_results=len(cases))
        # Keep the full comparison denominator, but also expose useful
        # comparisons before every tactic has completed. Each subset computes
        # its own common eligible cases; never compare unmatched raw totals.
        available = tuple(p for p in provers if any(p in rows for rows in cases.values()))
        report['available_provers'] = list(available)
        report['available_comparison'] = summarize(available)[0] if available else None
        report['pairwise_comparisons'] = [summarize((first, second))[0]
            for i, first in enumerate(available) for second in available[i + 1:]]
        target = out / f'comparison-search{budget}.json'
        save_json(target, report)
        with (out / f'comparison-search{budget}.tsv').open('w') as stream:
            stream.write('theorem\teligible\t' + '\t'.join(provers) + '\n')
            for task, rows in sorted(common.items()):
                theorem = next((row.get('extracted', {}).get('theorem') for row in rows.values()
                                if row.get('extracted', {}).get('theorem')), task)
                stream.write(f'{theorem}\t{task in eligible}\t' + '\t'.join(rows[p]['status'] for p in provers) + '\n')
        print(f'Comparison on {len(eligible)} common eligible theorems: {report["proved"]}; details: {target}')
        if available != provers and available:
            partial = report['available_comparison']
            print(f'Available tactics on {partial["common_eligible"]} common eligible theorems: {partial["proved"]}')
        for pair in report['pairwise_comparisons']:
            print(f'Paired comparison on {pair["common_eligible"]} common eligible theorems: {pair["proved"]}')


def collect(ns):
    print('Reading manifest...', flush=True)
    cfg = json.loads(Path(ns.manifest).read_text())
    if 'repetition_manifests' in cfg:
        return collect_repeated(ns, cfg)
    out = Path(cfg['output'])
    kind = tool_kind(cfg)
    completed = []
    missing = []
    total = len(cfg['jobs'])
    print(f'Reading completed results ({total} expected runs)...', flush=True)
    results = read_results(out, bulk_only=cfg.get('result_format') == 'bulk',
                           progress=lambda count: print(f'  Read {count} completed results.', flush=True))
    print('Grouping results...', flush=True)
    for i, job in enumerate(cfg['jobs']):
        if i not in results:
            missing.append(i)
        else:
            completed.append((job, results[i], i))
    if missing and not ns.partial:
        raise ValueError(f'{len(missing)} runs lack results (examples: {missing[:10]}). '
                         'Wait for the Slurm array to finish or use --partial.')
    groups = {}
    for job, data, idx in completed:
        groups.setdefault(job['runset'], []).append((job, data, idx))
    stamp = dt.datetime.now().astimezone().isoformat()
    for runset, runs in groups.items():
        detailed = kind == 'hammer' and all(job_prover(job) == 'vampire-hammer' for job, _, _ in runs)
        hammer_columns = HAMMER_COLUMNS if detailed else COMPARISON_COLUMNS
        print(f'Writing XML for {runset} ({len(runs)} runs)...', flush=True)
        root = ET.Element('result', {
            'benchmarkname': cfg['name'], 'name': runset, 'date': stamp, 'starttime': cfg['created'],
            'endtime': stamp, 'tool': cfg.get('executable', cfg.get('command', 'vampire')), 'toolmodule': 'slurmbench',
            'version': 'external', 'generator': 'slurmbench',
            'timelimit': f'{cfg["seconds"]}s', 'memlimit': f'{cfg["mem_mb"]} MB',
            'cpuCores': str(cfg['cpus'])})
        cols = ET.SubElement(root, 'columns')
        titles = ['status', 'cputime', 'walltime', 'memory', 'exitcode']
        if kind == 'vampire':
            titles.extend(['szs-status', 'instruction-count'])
        elif kind == 'hammer':
            titles.extend(hammer_columns)
        for title in titles:
            ET.SubElement(cols, 'column', {'title': title})
        for host in sorted(set(data['hostname'] for _, data, _ in runs)):
            si = ET.SubElement(root, 'systeminfo', {'hostname': host})
            ET.SubElement(si, 'os', {'name': 'Linux'})
            ET.SubElement(si, 'cpu', {'model': 'unknown (see Slurm)', 'cores': str(cfg['cpus'])})
            ET.SubElement(si, 'ram', {'size': 'unknown'})
            ET.SubElement(si, 'environment')
        for job, data, idx in runs:
            location = data.get('log_storage')
            logfile = f'{location["path"]}#{location["offset"]}:{location["length"]}' if location else f'logs/{idx:06d}.log'
            run = ET.SubElement(root, 'run', {'name': job['input'], 'files': f"[\"{job['input']}\"]",
                                                'options': shlex.join(job['args'] if job.get('theorem') else job['args'][:-1]),
                                                'logfile': logfile})
            column(run, 'status', data['status'])
            if data.get('cputime') is not None:
                column(run, 'cputime', f"{data['cputime']:.6f}s")
            column(run, 'walltime', f"{data['walltime']:.6f}s")
            if data['memory'] is not None:
                column(run, 'memory', f"{data['memory']}B")
            if data['returncode'] is not None:
                column(run, 'exitcode', data['returncode'])
            if kind == 'vampire':
                extracted = data.get('extracted', {})
                if extracted.get('szs-status') is not None:
                    column(run, 'szs-status', extracted['szs-status'])
                column(run, 'instruction-count', extracted.get('instruction-count', '0'))
            elif kind == 'hammer':
                extracted = data.get('extracted', {})
                for title in hammer_columns:
                    if extracted.get(title) not in (None, ''):
                        column(run, title, extracted[title])
        safe = re.sub(r'[^A-Za-z0-9_.-]+', '_', runset)
        target = out / f'{cfg["name"]}.{safe}.results.xml'
        tree = ET.ElementTree(root)
        ET.indent(tree, space='  ')
        tree.write(target, encoding='utf-8', xml_declaration=True)
        print(target)
        if detailed:
            print(f'Writing diagnostics for {runset}...', flush=True)
            hammer_diagnostics(out, runset, runs)
        else:
            print('  status:', dict(Counter(data['status'] for _, data, _ in runs).most_common()))
    print(f'Collected {len(completed)}/{len(cfg["jobs"])} runs; missing {len(missing)}.')
    if kind == 'hammer':
        print('Writing comparison diagnostics...', flush=True)
        comparison_diagnostics(out, completed, {job['args'][job['args'].index('--prover') + 1]
                                              for job in cfg['jobs'] if '--prover' in job['args']})
    if missing:
        print('Missing indices:', ', '.join(map(str, missing[:30])))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='action', required=True)
    s = sub.add_parser('submit', help='Submit array for XML benchmark')
    s.add_argument('benchmark')
    s.add_argument('--repetitions', type=int, default=1, help='Separate sequential job arrays for repeated runs (e.g. 3)')
    s.add_argument('-p', '--partition', default='CPU-amd')
    s.add_argument('-j', '--parallel', type=int, default=4, help='Maximum concurrently running array tasks')
    s.add_argument('-o', '--output')
    s.add_argument('--slack', type=int, default=2, help='Extra Slurm walltime minutes for startup')
    s.add_argument('--sbatch-arg', action='append', help='Extra sbatch option, e.g. --sbatch-arg=--account=abc')
    s.set_defaults(func=submit)
    sp = sub.add_parser('submit-packed', help='Fixed nodes, parallel one-core runs per node')
    sp.add_argument('benchmark')
    sp.add_argument('--repetitions', type=int, default=1, help='Separate sequential job arrays for repeated runs; implies --job-array when greater than 1')
    sp.add_argument('-p', '--partition', default='CPU-amd')
    sp.add_argument('--nodes', type=int, default=2)
    sp.add_argument('--cpus-per-node', type=int, default=8)
    sp.add_argument('--require-no-boost-no-smt', action='store_true', help='Record boost/SMT sysfs controls and refuse to run unless boost=0 and SMT=off/forceoff/notsupported; implies --physical-cores')
    sp.add_argument('--physical-cores', action='store_true', help='Select one logical CPU per verified physical core; fail if insufficient topology/cores')
    sp.add_argument('--shards', type=int, default=1, help='Number of packed shards (separate jobs, or array elements with --job-array)')
    sp.add_argument('--job-array', action='store_true', help='Submit packed shards as elements of one Slurm job array')
    sp.add_argument('--array-parallel', type=int, help='Maximum concurrently running packed array elements')
    sp.add_argument('-o', '--output')
    sp.add_argument('--slack', type=int, default=5, help='Extra walltime minutes for the whole batch')
    sp.add_argument('--mem', help='Memory per node job (sbatch --mem, e.g. 200G); default memlimit × cpus-per-node')
    sp.add_argument('--time', type=int, help='Walltime minutes per job instead of the worst case from timelimit')
    sp.add_argument('--batch-by-module', type=int, default=1, metavar='N',
                    help='Replay each defining module once for up to N consecutive theorems of the same '
                         'tactic (lean-auto-style suites with module metadata); results stay per theorem')
    sp.add_argument('--scratch-root', help='Stage hammer checkout and Lean toolchain once per node under this existing directory (e.g. /scratch); requires rsync')
    sp.add_argument('--result-format', choices=('bulk', 'files'), default='bulk',
                    help='bulk (default): append node-local execution logs/results to two shared files per node worker; files: legacy per-run output')
    sp.add_argument('--sbatch-arg', action='append')
    sp.set_defaults(func=submit_packed)
    rs = sub.add_parser('resubmit-shard', help='Resubmit a packed shard; runs with usable results are skipped')
    rs.add_argument('manifest')
    rs.add_argument('shard', type=int)
    rs.add_argument('--sbatch-arg', action='append', help='Extra sbatch option, e.g. --sbatch-arg=--time=600')
    rs.set_defaults(func=resubmit_shard)
    pw = sub.add_parser('packed-worker', help='Internal: per-node worker')
    pw.add_argument('manifest')
    pw.add_argument('shard', nargs='?', type=int, default=0)
    pw.add_argument('--resume', action='store_true',
                    help='skip runs with a usable result; killed batches run again')
    pw.set_defaults(func=packed_worker)
    w = sub.add_parser('worker', help='Internal: Slurm array worker')
    w.add_argument('manifest')
    w.set_defaults(func=worker)
    c = sub.add_parser('collect', help='Generate BenchExec-compatible results XML')
    c.add_argument('manifest')
    c.add_argument('--partial', action='store_true')
    c.set_defaults(func=collect)
    log = sub.add_parser('show-log', help='Print a run log from bulk or legacy storage')
    log.add_argument('manifest')
    log.add_argument('idx', type=int)
    log.set_defaults(func=show_log)
    pack = sub.add_parser('pack-results', help='Consolidate a completed legacy run for faster repeated collection; retains originals')
    pack.add_argument('manifest')
    pack.set_defaults(func=pack_results)
    ns = p.parse_args()
    try:
        ns.func(ns)
    except (ValueError, OSError, ET.ParseError) as ex:
        p.error(str(ex))

if __name__ == '__main__':
    main()
