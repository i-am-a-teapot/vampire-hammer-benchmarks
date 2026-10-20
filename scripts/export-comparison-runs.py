#!/usr/bin/env python3
"""Export separate, unaveraged runs from a plot-comparison scope's case CSVs."""

import argparse
import csv
from contextlib import ExitStack
from itertools import zip_longest
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True,
                        help='scope directory containing comparison.csv and problems-TOOL.csv')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    with (args.source / 'comparison.csv').open(newline='') as stream:
        provers = [row['prover'] for row in csv.DictReader(stream)
                   if row['prover'] != 'theoretical-best']
    if not provers or len(set(provers)) != len(provers):
        parser.error('Expected distinct real tools in comparison.csv')
    args.output.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        readers = [csv.DictReader(stack.enter_context(
            (args.source / f'problems-{p}.csv').open(newline=''))) for p in provers]
        runs = [field.removesuffix('_status') for field in readers[0].fieldnames
                if field.startswith('run_') and field.endswith('_status')]
        if not runs:
            parser.error('No individual run columns found')
        required = {'theorem', *(f'{r}_{kind}' for r in runs for kind in ('status', 'seconds'))}
        if any(not required.issubset(reader.fieldnames) for reader in readers):
            parser.error('Case files have inconsistent run columns')
        writers = []
        paths = []
        solved = [{p: 0 for p in provers} for _ in runs]
        for run in runs:
            path = args.output / f'benchmark-{run.replace("_", "-")}.csv'
            paths.append(path)
            writer = csv.writer(stack.enter_context(path.open('w', newline='')))
            writer.writerow(['theorem', *(f'{p}_{kind}' for p in provers
                                         for kind in ('status', 'seconds'))])
            writers.append(writer)
        count = 0
        for rows in zip_longest(*readers):
            if any(row is None for row in rows):
                raise ValueError('Case files have different lengths')
            theorem = rows[0]['theorem']
            if any(row['theorem'] != theorem for row in rows):
                raise ValueError('Case files must have identical theorem order')
            for i, (run, writer) in enumerate(zip(runs, writers)):
                writer.writerow([theorem, *(row[f'{run}_{kind}'] for row in rows
                                           for kind in ('status', 'seconds'))])
                for p, row in zip(provers, rows):
                    solved[i][p] += row[f'{run}_status'] == 'true'
            count += 1
    for path, counts in zip(paths, solved):
        print(f'{path}: {count:,} theorems; solved counts: {counts}')


if __name__ == '__main__':
    main()
