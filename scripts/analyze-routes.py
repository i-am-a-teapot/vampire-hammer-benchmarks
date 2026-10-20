#!/usr/bin/env python3
"""Audit accepted proof routes and decomposition evidence in collected runs.

Reads gzip members addressed by the collected XML's logfile offsets. Counts
accepted-proof reconstruction markers, never infers a winning route from the
last attempted solver. Decomposition is reported separately from export mode.
"""

import argparse
from collections import Counter
import csv
import gzip
import json
from pathlib import Path
import re
import shlex
import statistics
import xml.etree.ElementTree as ET


def read_xml(path):
    rows = {}
    root = None
    for event, elem in ET.iterparse(path, events=('start', 'end')):
        if root is None:
            root = elem
        if event != 'end' or elem.tag != 'run':
            continue
        cols = {c.get('title'): c.get('value', '') for c in elem.findall('column')}
        option = re.search(r'(?:^| )--theorem (.+)$', elem.get('options', ''))
        task = re.search(r'#\d+-(.+)$', elem.get('name', ''))
        if option:
            raw = option[1]
            name = raw if not re.search(r"[\s'\"\\]", raw) else shlex.split(raw)[0]
        else:
            name = task[1] if task else cols['theorem']
        if name in rows:
            raise ValueError(f'Duplicate task: {name}')
        rows[name] = (cols, elem.get('logfile'))
        elem.clear()
        root.clear()
    return rows


def inspect_success(directory, name, cols, location, handles):
    file, span = location.rsplit('#', 1)
    offset, length = map(int, span.split(':'))
    if file not in handles:
        handles[file] = (directory / file).open('rb')
    stream = handles[file]
    stream.seek(offset)
    log = gzip.decompress(stream.read(length)).decode()
    results = [json.loads(line.removeprefix('HAMMER-RESULT: '))
               for line in log.splitlines() if line.startswith('HAMMER-RESULT: ')]
    result, = [r for r in results if r.get('name') == name]
    assert result['status'] == 'proved' and result['kernel_checked'], name
    modes = sorted(result['reconstruction_modes'])
    assert ','.join(modes) == cols.get('reconstruction', ''), name
    route = {(): 'direct', ('fo',): 'fo', ('ho',): 'ho',
             ('fo', 'ho'): 'mixed'}[tuple(modes)]
    events = [json.loads(line.removeprefix('[vampire.event] '))
              for line in log.splitlines() if line.startswith('[vampire.event] ')]
    assert all(e.get('name') == name for e in events), name
    counts = Counter(e['event'] for e in events)
    queries = result.get('query_details', [])
    branch_queries = [q for q in queries if '.branch-' in q.get('problem', '')]
    branch_refutations = [q for q in branch_queries
                          if q.get('szs_status') in ('Unsatisfiable', 'Theorem', 'ContradictoryAxioms')]
    # The reserve event marks entry into the fallback, not an accepted split.
    # Without branch artifacts, it cannot distinguish direct closure before
    # splitting from branches closed solely by simplification/direct proof.
    phase = ('branch-search-recorded' if branch_queries else
             'decomposition-entered-no-branch-search' if counts['decomposition_reserve'] else
             'no-decomposition-recorded')
    return dict(theorem=name, route=route, decomposition=phase,
                branch_queries=len(branch_queries), branch_refutations=len(branch_refutations),
                decomposition_entries=counts['decomposition_reserve'],
                empty_branches=counts['empty_branch'],
                native_retries=counts['native_search_retry'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('results', nargs='+', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    runs, populations = [], []
    for index, directory in enumerate(args.results, 1):
        print(f'Reading run {index}: {directory}', flush=True)
        sources = list(directory.glob('comparison-search*.json'))
        source, = sources
        config = json.loads(source.read_text())
        suffix = source.stem.removeprefix('comparison-')
        eligible = {}
        records = None
        for prover in config['provers']:
            print(f'  Loading {prover}', flush=True)
            rows = read_xml(directory / f'vampire-hammer.{prover}-{suffix}.results.xml')
            eligible[prover] = {n for n, (c, _) in rows.items() if c.get('outcome') != 'excluded'}
            if prover == 'vampire-hammer':
                records = rows
        populations.append(eligible)
        successes = {}
        handles = {}
        try:
            for name, (cols, location) in records.items():
                if cols['status'] == 'true':
                    successes[name] = inspect_success(directory, name, cols, location, handles)
        finally:
            for handle in handles.values():
                handle.close()
        runs.append(successes)
        print(f'Run {index}: audited {len(successes):,} accepted proofs', flush=True)
    provers = set(populations[0])
    assert all(set(p) == provers for p in populations)
    scopes = {'vampire-hammer-only': {'vampire-hammer'}, 'common-all': provers,
              'common-without-lean-smt': provers - {'lean-smt'}}
    summary = {'inputs': [str(p.resolve()) for p in args.results], 'scopes': {}}
    report = ['# Accepted proof routes and decomposition\n',
              'Route counts come from reconstruction markers in the accepted, kernel-checked proof. '
              '“Mixed” means that both FO and HO reconstructed proofs occur in that proof; '
              'it need not indicate structural splitting (inhabitation case analysis can also combine routes). '
              'Counts are observations of this schedule, not an ablation measuring which route is necessary.\n',
              'Decomposition is a separate dimension. Branch problem artifacts establish that external '
              'branch search was attempted. The decomposition-reserve event establishes entry into the '
              'fallback, but without branch artifacts does not establish an actual split: direct proof '
              'before splitting and branches closed entirely in Lean are not separately instrumented. '
              'Artifacts and events survive rollback, so they do not alone certify which branch searches '
              'contributed to the accepted proof.\n']
    for scope, selected in scopes.items():
        tasks = set.intersection(*(p[tool] for p in populations for tool in selected))
        counts = []
        for index, successes in enumerate(runs, 1):
            subset = [r for name, r in successes.items() if name in tasks]
            counter = Counter({'total': len(subset)})
            for row in subset:
                counter['route:' + row['route']] += 1
                counter['decomposition:' + row['decomposition']] += 1
                counter[row['decomposition'] + ':' + row['route']] += 1
                if row['branch_refutations']:
                    counter['has-branch-refutation'] += 1
                if row['empty_branches']:
                    counter['has-empty-branch'] += 1
            counts.append(counter)
            with (args.output / f'{scope}-run-{index}.csv').open('w') as stream:
                fields = list(next(iter(successes.values())))
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                writer.writerows(sorted(subset, key=lambda r: r['theorem']))
        keys = sorted(set().union(*(set(c) for c in counts)))
        values = {key: {'per_run': [c[key] for c in counts],
                        'mean': statistics.mean(c[key] for c in counts)} for key in keys}
        summary['scopes'][scope] = dict(common_eligible=len(tasks), counts=values)
        report += [f'## {scope}\n', f'{len(tasks):,} matched eligible theorems.\n',
                   '| Category | ' + ' | '.join(f'Run {i}' for i in range(1, len(runs)+1)) + ' | Mean |',
                   '| --- | ' + ' | '.join('---:' for _ in runs) + ' | ---: |']
        for key in keys:
            value = values[key]
            report.append('| ' + key + ' | ' + ' | '.join(f'{n:,}' for n in value['per_run']) +
                          f" | {value['mean']:,.1f} |")
        report.append('')
    (args.output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    (args.output / 'report.md').write_text('\n'.join(report) + '\n')
    print(f'Wrote {args.output / "report.md"}', flush=True)


if __name__ == '__main__':
    main()
