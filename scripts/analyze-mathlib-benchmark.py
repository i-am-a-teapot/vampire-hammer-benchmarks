#!/usr/bin/env python3
"""Analyze saved mathlib benchmark outcomes, exports, and live phase timings.

Read-only with respect to run evidence. Writes analysis.json and parallel TSV
views into RUN/analysis (or --output). Supports partial runs explicitly.
"""
import argparse
from collections import Counter, defaultdict
import csv
import json
from pathlib import Path
import re
import statistics
import time


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def distribution(values):
    values = sorted(values)
    if not values:
        return {'count': 0, 'sum': 0}
    return {'count': len(values), 'sum': round(sum(values), 3),
            'median': round(statistics.median(values), 3),
            'p90': round(values[max(0, (9 * len(values) + 9) // 10 - 1)], 3),
            'max': round(values[-1], 3)}


def error_category(row):
    kind = row['failure_kind']
    message = row.get('error', '').lower()
    failure_expression = message.split('this is because', 1)[0]
    if 'auto.lamreif.reiftermchecktype' in message:
        return 'reification_type_error'
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
    if 'heartbeats' in message:
        return 'heartbeat_limit'
    if 'proof-search budget exhausted' in message:
        return 'search_budget_exhausted'
    if 'recursion' in message:
        return 'recursion_limit'
    if kind == 'search_unsuccessful':
        return 'satisfiable' if 'szs status satisfiable' in message else 'other_search_unsuccessful'
    return kind


def tsv(path, data, keys):
    with path.open('w') as stream:
        writer = csv.DictWriter(stream, keys, delimiter='\t', extrasaction='ignore')
        writer.writeheader()
        writer.writerows(data)


def analyze(run, output, previous):
    selected = {r['name']: r for r in rows(run / 'lemmas.jsonl')}
    for attempt in range(20):
        try:
            results = rows(run / 'results.jsonl')
            summary = json.loads((run / 'summary.json').read_text())
            if summary['attempted'] == len(results):
                break
        except json.JSONDecodeError:
            pass
        time.sleep(0.05)
    else:
        raise RuntimeError('Could not capture a consistent live result/summary snapshot')
    assert len({r['name'] for r in results}) == len(results), 'duplicate names'
    assert len({r['id'] for r in results}) == len(results), 'duplicate IDs'
    assert all(r['name'] in selected and r['id'] == selected[r['name']]['id'] for r in results)
    config = json.loads((run / 'config.json').read_text())
    details, timings = [], defaultdict(list)
    for row in results:
        lemma = selected[row['name']]
        item = {'id': row['id'], 'name': row['name'], 'module': lemma['module'],
                'type_size': lemma['type_size'], 'status': row['status'],
                'failure_kind': row['failure_kind'], 'category': error_category(row),
                'seconds': row.get('seconds', 0), 'problem_bytes': 0, 'assertions': 0,
                'record_retry': False, 'exported': bool(row.get('problem')),
                'nat_theory': False, 'unused_nat_add_theory': False,
                'error': row.get('error', ''), 'log': row.get('log')}
        timings[item['failure_kind']].append(item['seconds'])
        if row.get('problem'):
            problem = Path(row['problem'])
            text = problem.read_text()
            item['problem_bytes'] = problem.stat().st_size
            item['assertions'] = len(re.findall(r'(?m)^t[fh]f\([^\n]*?,\s*(?:axiom|hypothesis|conjecture|negated_conjecture)\s*,', text))
            item['record_retry'] = Path(str(problem) + '.with-record-laws').exists()
            item['nat_theory'] = 'tff(nat_add_comm,' in text
            item['unused_nat_add_theory'] = item['nat_theory'] and all('nat_add' not in line for line in text.splitlines() if line.startswith('tff(fact'))
        if row['failure_kind'] in {'process_error', 'infrastructure_error', 'timeout'} and row.get('source') and row.get('log'):
            source_path = Path(row['source'])
            command_lines = [i+1 for i,line in enumerate(source_path.read_text().splitlines())
                             if line.startswith('hammer_mathlib_try ') and json.loads(line.split(' recording ',1)[0].removeprefix('hammer_mathlib_try ')) == row['name']]
            for line in Path(row['log']).read_text().splitlines():
                match = re.match(re.escape(str(source_path)) + r':(\d+):\d+: error: (.*)', line)
                if match and any(pos-2 <= int(match[1]) <= pos for pos in command_lines):
                    item['diagnostic_error'] = match[2]
                    if 'heartbeats' in match[2]: item['category'] = 'heartbeat_limit'
                    elif 'recursion' in match[2]: item['category'] = 'recursion_limit'
                    break
        details.append(item)
    phase_values = defaultdict(list)
    node_rules = defaultdict(list)
    process_errors = []
    for log in sorted((run / 'chunks').glob('*.log')):
        text = log.read_text()
        for ms, phase in re.findall(r'^\[vampire.profile\] (\d+) ms: (.+)$', text, re.M):
            if phase.startswith('node '):
                rule = phase.rsplit('(', 1)[-1].removesuffix(')')
                node_rules[rule].append(int(ms) / 1000)
            elif not phase.startswith('  cnfify '):
                phase_values[phase].append(int(ms) / 1000)
        for line in text.splitlines():
            if re.search(r':\d+:\d+: error:', line):
                process_errors.append({'log': str(log), 'message': line})
    by_module = defaultdict(Counter)
    for item in details:
        bucket = '.'.join(item['module'].split('.')[:2])
        by_module[bucket][item['failure_kind']] += 1
    old_comparisons = []
    current = {r['name']: r for r in results}
    for old_path in previous:
        old = {r['name']: r for r in rows(old_path / 'results.jsonl')}
        overlap = current.keys() & old.keys()
        old_config = json.loads((old_path / 'config.json').read_text())
        old_proofs = {n for n in overlap if old[n]['status'] == 'proved'}
        new_proofs = {n for n in overlap if current[n]['status'] == 'proved'}
        old_comparisons.append({'run': str(old_path), 'overlap': len(overlap),
            'same_source_snapshot': (run/'source-hashes.json').read_bytes() == (old_path/'source-hashes.json').read_bytes(),
            'previous_search_seconds': old_config.get('search_seconds', 5),
            'preserved_proofs': len(old_proofs & new_proofs),
            'gained_proofs': sorted(new_proofs - old_proofs),
            'lost_proofs': sorted(old_proofs - new_proofs),
            'transitions': dict(Counter(old[n]['failure_kind'] + ' -> ' + current[n]['failure_kind'] for n in overlap))})
    report = {'selected': len(selected), 'attempted': len(results),
        'complete': len(results) == len(selected), 'config': config,
        'failure_kinds': dict(Counter(r['failure_kind'] for r in results)),
        'categories': dict(Counter(d['category'] for d in details)),
        'case_seconds_by_outcome': {k: distribution(v) for k,v in timings.items()},
        'phase_seconds': {k: distribution(v) for k,v in phase_values.items()},
        'replay_node_seconds_by_rule': {k: distribution(v) for k,v in node_rules.items()},
        'phase_timing_caveat': 'Completed spans only; failed phases may have no duration. Parent/child replay spans overlap. Do not sum all phases as exclusive time.',
        'search_timing_caveat': 'Per-worker % Time elapsed is not total portfolio time. Case seconds include translation, replay, kernel and dependency checks.',
        'case_wall_seconds_sum': round(sum(d['seconds'] for d in details), 3),
        'run_wall_seconds': summary['run_wall_seconds'],
        'exported_cases': sum(d['exported'] for d in details),
        'independent_proofs_without_vampire_export': sum(d['status']=='proved' and not d['exported'] for d in details),
        'independent_proofs_with_vampire_export': sum(d['status']=='proved' and d['exported'] for d in details),
        'export_size_by_outcome': {kind: distribution([d['problem_bytes'] for d in details if d['failure_kind']==kind and d['exported']]) for kind in sorted(timings)},
        'assertion_count_by_outcome': {kind: distribution([d['assertions'] for d in details if d['failure_kind']==kind and d['exported']]) for kind in sorted(timings)},
        'record_retry_cases': sum(d['record_retry'] for d in details),
        'nat_theory_cases': sum(d['nat_theory'] for d in details),
        'unused_nat_add_theory_cases': sum(d['unused_nat_add_theory'] for d in details),
        'unused_nat_add_theory_by_outcome': dict(Counter(d['failure_kind'] for d in details if d['unused_nat_add_theory'])),
        'small_sat_exports': [d for d in details if d['category']=='satisfiable' and d['assertions']<=3],
        'record_retries_by_outcome': dict(Counter(d['failure_kind'] for d in details if d['record_retry'])),
        'outcomes_by_mathlib_area': dict(by_module),
        'comparisons': old_comparisons,
        'slowest_cases': sorted(details, key=lambda d:d['seconds'], reverse=True)[:40],
        'largest_exports': sorted(details, key=lambda d:d['problem_bytes'], reverse=True)[:40],
        'process_error_messages': process_errors}
    output.mkdir(parents=True, exist_ok=True)
    (output/'analysis.json').write_text(json.dumps(report, indent=2)+'\n')
    tsv(output/'cases.tsv', sorted(details,key=lambda d:d['id']),
        ['id','name','module','type_size','status','failure_kind','category','seconds','exported','problem_bytes','assertions','record_retry','unused_nat_add_theory','error','log'])
    tsv(output/'slowest.tsv', report['slowest_cases'],
        ['id','name','failure_kind','seconds','problem_bytes','assertions','record_retry'])
    for category in sorted({d['category'] for d in details}):
        (output/(category+'-selection.txt')).write_text(''.join(d['name']+'\n' for d in details if d['category']==category))
    print(json.dumps({key:report[key] for key in ['selected','attempted','complete','categories','case_wall_seconds_sum','exported_cases','record_retry_cases']},indent=2))


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run',type=Path)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--previous',type=Path,action='append',default=[])
    args=parser.parse_args()
    analyze(args.run,args.output or args.run/'analysis',args.previous)
