#!/usr/bin/env python3
"""Prepare a reproducible sample, then run BenchExec as the container process."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET


def positive(value):
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError('must be positive')
    return result


def parse_args(argv=None):
    root = Path(os.environ.get('BENCHMARK_ROOT', '/opt/benchmark')).resolve()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--definition', type=Path,
                        default=os.environ.get('BENCHMARK_DEFINITION', str(root / 'configurations/normal.xml')),
                        help='experiment XML (default: image-selected experiment)')
    parser.add_argument('--count', type=positive, default=os.environ.get('BENCHMARK_COUNT', '100'),
                        help='number of sampled theorems (default: 100, or image/environment setting)')
    parser.add_argument('--seed', default=os.environ.get('BENCHMARK_SEED', 'mathlib-v4.34.1-docker'))
    parser.add_argument('--jobs', type=positive, default=os.environ.get('BENCHMARK_JOBS', '1'),
                        help='parallel BenchExec processes (default: 1, or image/environment setting)')
    parser.add_argument('--output', type=Path, default=os.environ.get('BENCHMARK_OUTPUT', '/results/run'))
    parser.add_argument('--prover', action='append', help='select matching experiment provers (repeatable)')
    parser.add_argument('--extract-only', action='store_true', help='export the sample without running BenchExec')
    parser.add_argument('--benchexec-arg', action='append', default=[],
                        help='additional BenchExec argument, e.g. --benchexec-arg=--no-hyperthreading')
    args = parser.parse_args(argv)
    return parser, args


def main(argv=None):
    parser, args = parse_args(argv)
    root = Path(os.environ.get('BENCHMARK_ROOT', '/opt/benchmark')).resolve()
    definition = args.definition.expanduser().resolve()
    output = args.output.expanduser().resolve()
    try:
        experiment = ET.parse(definition).getroot()
    except (OSError, ET.ParseError) as error:
        parser.error(str(error))
    if experiment.tag != 'benchmark':
        parser.error('the experiment definition must have a <benchmark> root')
    if output.exists():
        parser.error(f'output already exists: {output}; select a new --output directory')
    selection = {option.get('name') for tasks in experiment.findall('tasks') for option in tasks.findall('option')}
    command = [sys.executable, str(root / 'scripts/make-suite.py'),
               '--definition', str(definition), '--backends', str(root / 'artifacts/provers/backends.json'),
               '--format', 'benchexec', '--count', str(args.count), '--seed', args.seed,
               '--output', str(output / 'suite')]
    if not selection.intersection({'--catalog', '--test-suite'}):
        command += ['--catalog', os.environ.get('BENCHMARK_CATALOG', '/opt/benchmark-data/catalog.jsonl')]
    for prover in args.prover or []:
        command += ['--prover', prover]
    output.mkdir(parents=True)
    print(f'Preparing up to {args.count} testcases with {definition}', flush=True)
    code = subprocess.run(command, check=False).returncode
    if code:
        return code
    if args.extract_only:
        return 0
    results = output / 'results'
    results.mkdir()
    command = ['benchexec', '--no-container', '--tool-directory', str(root),
               '--numOfThreads', str(args.jobs), '--outputpath', str(results) + '/',
               *args.benchexec_arg, str(output / 'suite/vampire-hammer.xml')]
    (output / 'benchexec-command.json').write_text(json.dumps(command, indent=2) + '\n')
    environment = dict(os.environ)
    environment['PYTHONPATH'] = str(root / 'scripts') + (
        os.pathsep + environment['PYTHONPATH'] if environment.get('PYTHONPATH') else '')
    # No supervising process remains in the container's cgroup. BenchExec owns
    # signals, exit status, child processes, resource accounting and result files.
    print(f'Running BenchExec with {args.jobs} worker(s); results: {results}', flush=True)
    try:
        os.execvpe(command[0], command, environment)
    except OSError as error:
        parser.error(f'could not start BenchExec: {error}')


if __name__ == '__main__':
    sys.exit(main())
