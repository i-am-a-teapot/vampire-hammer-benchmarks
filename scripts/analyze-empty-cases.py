#!/usr/bin/env python3
"""Audit empty-domain recovery in packed benchmark logs without inferring hidden splits."""

import argparse
from collections import Counter
import csv
import gzip
import json
from pathlib import Path
import runpy


def analyze(events):
    counts = Counter(e['event'] for e in events)
    branches = []
    stack, active = [], {}
    rollback = bool(counts['deferred_witness_failed'])
    # Recovery start/end events nest. Keep ancestor empty obligations open
    # across nested recoveries; a child's discharge does not finish its parent.
    for position, event in enumerate(events):
        kind = event['event']
        if kind == 'assuming_nonempty':
            stack.append(position)
        elif kind == 'empty_branch':
            owner = stack[-1] if stack else -1
            if owner in active:
                active.pop(owner)['terminal'] = 'next_empty_branch_same_recovery'
            branch = dict(start=position, terminal='unrecorded', exported={}, solver_results=0,
                          rollback_ambiguity=rollback or owner == -1)
            branches.append(branch)
            active[owner] = branch
        elif kind in ('domain_assumptions_discharged', 'deferred_witness_failed'):
            if stack:
                owner = stack.pop()
                if owner in active:
                    active.pop(owner)['terminal'] = kind
        elif kind == 'exported':
            route = 'ho' if event['higher_order'] else 'fo'
            for branch in active.values():
                branch['exported'][route] = branch['exported'].get(route, 0) + 1
        elif kind == 'solver_result':
            for branch in active.values():
                branch['solver_results'] += 1
    return counts, branches


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('results', type=Path, nargs='+')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    read_xml = runpy.run_path(str(Path(__file__).with_name('analyze-routes.py')))['read_xml']
    summaries = []
    for index, directory in enumerate(args.results, 1):
        xml, = directory.glob('vampire-hammer.vampire-hammer-search*.results.xml')
        rows = read_xml(xml)
        files = {}
        stats = {'all-eligible': Counter(), 'solved': Counter()}
        records = []
        details = []
        try:
            for name, (cols, location) in rows.items():
                if cols.get('outcome') == 'excluded':
                    continue
                file, span = location.rsplit('#', 1)
                offset, length = map(int, span.split(':'))
                if file not in files:
                    files[file] = (directory / file).open('rb')
                handle = files[file]
                handle.seek(offset)
                log = gzip.decompress(handle.read(length)).decode()
                events = [json.loads(line.removeprefix('[vampire.event] '))
                          for line in log.splitlines() if line.startswith('[vampire.event] ')]
                assert all(e.get('name') == name for e in events), name
                counts, branches = analyze(events)
                solved = cols.get('status') == 'true'
                row = dict(theorem=name, solved=solved,
                           accepted_route=cols.get('reconstruction') or cols.get('route'),
                           assuming_nonempty=counts['assuming_nonempty'],
                           recoveries_discharged=counts['domain_assumptions_discharged'],
                           known_empty_domain=counts['known_empty_domain'],
                           residual_empty_branches=len(branches),
                           residual_with_export=sum(bool(b['exported']) for b in branches),
                           residual_without_export=sum(not b['exported'] for b in branches),
                           residual_lean_completed=sum(not b['exported'] and b['terminal'] in
                                                       ('domain_assumptions_discharged',
                                                        'next_empty_branch_same_recovery') and not
                                                       b['rollback_ambiguity'] for b in branches),
                           residual_external_completed=sum(bool(b['exported']) and b['terminal'] in
                                                           ('domain_assumptions_discharged',
                                                            'next_empty_branch_same_recovery') and not
                                                           b['rollback_ambiguity'] for b in branches),
                           residual_failed=sum(b['terminal'] == 'deferred_witness_failed' for b in branches),
                           residual_rollback_ambiguous=sum(b['rollback_ambiguity'] for b in branches),
                           residual_unfinished=sum(b['terminal'] == 'unrecorded' for b in branches))
                for scope in ('all-eligible', 'solved') if solved else ('all-eligible',):
                    s = stats[scope]
                    s['theorems'] += 1
                    s['theorems_assuming_nonempty'] += bool(row['assuming_nonempty'])
                    s['theorems_discharged_no_residual'] += bool(row['recoveries_discharged']) and not branches
                    s['theorems_with_residual'] += bool(branches)
                    s['theorems_with_residual_export'] += bool(row['residual_with_export'])
                    s['theorems_with_residual_but_no_export'] += bool(branches) and not row['residual_with_export']
                    s['theorems_with_known_empty'] += bool(row['known_empty_domain'])
                    s['theorems_residual_with_rollback_ambiguity'] += bool(row['residual_rollback_ambiguous'])
                    for key in ('assuming_nonempty', 'recoveries_discharged', 'residual_empty_branches',
                                'residual_with_export', 'residual_without_export', 'residual_lean_completed',
                                'residual_external_completed', 'residual_failed', 'residual_unfinished',
                                'residual_rollback_ambiguous'):
                        s[key] += row[key]
                if branches or counts['assuming_nonempty'] or counts['known_empty_domain']:
                    records.append(row)
                if branches:
                    details.append(dict(theorem=name, solved=solved, branches=branches, events=events))
        finally:
            for handle in files.values():
                handle.close()
        with (args.output / f'empty-cases-run-{index}.csv').open('w') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(records[0]))
            writer.writeheader()
            writer.writerows(records)
        (args.output / f'empty-case-events-run-{index}.json').write_text(json.dumps(details, indent=2)+'\n')
        summary = dict(input=str(directory.resolve()), counts=stats)
        summaries.append(summary)
        print(json.dumps(summary), flush=True)
    (args.output / 'empty-cases-summary.json').write_text(json.dumps(summaries, indent=2)+'\n')


if __name__ == '__main__':
    main()
