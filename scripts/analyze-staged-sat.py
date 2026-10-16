#!/usr/bin/env python3
"""Read staged journals and their indexed logs without modifying the source."""
import argparse
import collections
import csv
import gzip
import json
import pathlib
import re

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('source', type=pathlib.Path)
parser.add_argument('output', type=pathlib.Path)
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=True)
baseline = {}
with (args.source / 'comparison-search10.tsv').open() as f:
    for row in csv.DictReader(f, delimiter='\t'):
        baseline[row['theorem']] = row
stats = collections.Counter()
categories = collections.defaultdict(collections.Counter)
signals = collections.Counter()
errors = collections.Counter()
examples = collections.defaultdict(list)
snapshots = []
seen = set()
files = {}
sat_rows = []
status_re = re.compile(r'% SZS status Satisfiable\b')
with (args.output / 'cases.jsonl').open('w') as output:
    for journal in sorted((args.source / 'journals').glob('*.jsonl')):
        stat = journal.stat()
        snapshots.append((str(journal), stat.st_size, stat.st_mtime_ns))
        with journal.open() as f:
            for line in f:
                r = json.loads(line)
                e = r['extracted']
                if e.get('prover') != 'vampire-hammer':
                    continue
                assert r['idx'] not in seen, r['idx']
                seen.add(r['idx'])
                stats['vampire_runs'] += 1
                queries = e.get('query-details') or []
                prechecks = [q for q in queries if status_re.search(q.get('sat_precheck_stdout') or '')]
                searches = [q for q in queries if q.get('szs_status') == 'Satisfiable' or status_re.search(q.get('stdout_tail') or '')]
                logref = r.get('log_storage')
                log = ''
                if logref:
                    path = args.source / logref['path']
                    if path not in files:
                        files[path] = path.open('rb')
                        st = path.stat()
                        snapshots.append((str(path), st.st_size, st.st_mtime_ns))
                    stream = files[path]
                    stream.seek(logref['offset'])
                    data = stream.read(logref['length'])
                    log = (gzip.decompress(data) if logref.get('encoding') == 'gzip' else data).decode('utf-8')
                precheck_log_sat = 'SZS status Satisfiable); continuing with native HO' in log
                log_sat = 'staged FO failed; continuing with native HO: Vampire did not produce a refutation (SZS status Satisfiable)' in log
                if not prechecks and not searches and not log_sat:
                    continue
                precheck_sat = bool(prechecks) or precheck_log_sat
                stats['sat_cases'] += 1
                stats['log_only_sat_cases'] += bool(log_sat and not prechecks and not searches)
                stats['precheck_sat_queries_saved'] += len(prechecks)
                stats['search_sat_queries_saved'] += len(searches)
                stats['precheck_sat_cases'] += precheck_sat
                stats['search_sat_cases_saved'] += bool(searches)
                stats['both_sat_cases_saved'] += bool(precheck_sat and searches)
                b = baseline[e['theorem']]
                solved = [k for k in ('duper', 'aesop', 'simp_all') if b[k] == 'true']
                category = e['category']
                categories[category]['cases'] += 1
                categories[category]['baseline_any'] += bool(solved)
                categories[category]['precheck_sat'] += precheck_sat
                categories[category]['search_sat_saved'] += bool(searches)
                for k in solved:
                    categories[category][k] += 1
                flags = {
                    'whole_automatic_set_retry': 'Retrying unsupported translation without ' in log and 'automatic premises' in log,
                    'object_encoding_retry': 'checked function/proposition object encoding' in log,
                    'dropped_auxiliary_fact': 'Dropping auxiliary fact' in log,
                    'ho_search_started': 'starting staged HO search' in log,
                    'ho_search_completed': 'staged HO search' in (e.get('phases') or {}),
                    'fo_sat_ho_export_error': precheck_sat and ('THF export:' in (e.get('error-full') or e.get('error') or '')),
                }
                for key, val in flags.items():
                    signals[key] += val
                error = e.get('error-full') or e.get('error') or ''
                if error:
                    errors[error.splitlines()[0]] += 1
                row = {'theorem': e['theorem'], 'idx': r['idx'], 'category': category,
                       'outcome': e.get('outcome'), 'eligible': b['eligible'], 'baseline_proofs': solved,
                       'precheck_sat_queries': len(prechecks), 'search_sat_queries': len(searches),
                       'precheck_sat': precheck_sat, 'log_sat': log_sat,
                       'sat_search_problems': [q.get('problem') for q in searches],
                       'assertions': e.get('assertions'), 'last_phase': e.get('last-phase'),
                       'error': error, 'signals': flags, 'log_storage': logref}
                output.write(json.dumps(row, ensure_ascii=False) + '\n')
                sat_rows.append(row)
                group = category + ('-baseline' if solved else '-no-baseline')
                if len(examples[group]) < 5:
                    examples[group].append(row)
                    name = re.sub(r'[^\w.-]', '_', e['theorem'])
                    (args.output / (str(r['idx']) + '-' + name + '.log')).write_text(log)
for stream in files.values():
    stream.close()
assert all(pathlib.Path(p).stat().st_size == size and pathlib.Path(p).stat().st_mtime_ns == stamp
           for p, size, stamp in snapshots), 'Source changed during analysis'
with (args.output / 'cases.tsv').open('w') as f:
    cols = ['theorem', 'idx', 'category', 'eligible', 'baseline_proofs', 'precheck_sat_queries', 'search_sat_queries', 'assertions', 'last_phase']
    writer = csv.DictWriter(f, cols, delimiter='\t', extrasaction='ignore')
    writer.writeheader()
    for row in sat_rows:
        writer.writerow({**row, 'baseline_proofs': ','.join(row['baseline_proofs'])})
summary = {'counts': dict(stats), 'categories': {k: dict(v) for k, v in categories.items()},
           'log_signals': dict(signals), 'top_errors': errors.most_common(40),
           'examples': dict(examples), 'source_unchanged': True,
           'source_snapshots': snapshots}
(args.output / 'summary.json').write_text(json.dumps(summary, indent=2, ensure_ascii=False))
print(json.dumps({k: summary[k] for k in ('counts', 'categories', 'log_signals', 'top_errors')}, indent=2))
