#!/usr/bin/env python3
"""Run the extracted benchmark's Python and Lean regressions."""
import argparse
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import unittest

PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('benchmark_mathlib', PROJECT / 'scripts/benchmark-mathlib.py')
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('files', nargs='*', type=Path)
    parser.add_argument('--suite', choices=('python', 'lean', 'all'), default='python')
    parser.add_argument('--vampire', type=Path, default=benchmark.vampire_executable())
    parser.add_argument('--timeout', type=float, default=180)
    parser.add_argument('--list', action='store_true')
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error('--timeout must be positive')
    passed = True
    if args.suite in ('python', 'all') and not args.files:
        tests = unittest.TestSuite()
        for relative in ('Tests/Python', 'slurmrunner'):
            directory = str(PROJECT / relative)
            tests.addTests(unittest.TestLoader().discover(directory, top_level_dir=directory))
        if args.list:
            print(f'{tests.countTestCases()} Python tests')
        else:
            passed = unittest.TextTestRunner(verbosity=1).run(tests).wasSuccessful()
    if args.suite in ('lean', 'all') or args.files:
        files = args.files or [Path('Tests/Frontend/MathlibBenchmarkTests.lean'),
                              Path('slurmrunner/tests/ComparisonBenchmarkTests.lean')]
        for path in files:
            if args.list:
                print(path)
                continue
            command = ['lake', '-R', '-KbuildVampire=false', 'lean', str(path), '--', '-j', '1',
                       f'-Dweak.vampire.executable={args.vampire.resolve()}']
            environment = dict(os.environ, VAMPIRE_BIN=str(args.vampire.resolve()))
            process = subprocess.Popen(command, cwd=PROJECT, start_new_session=True, env=environment)
            try:
                code = process.wait(timeout=args.timeout)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise
            print(f'{"PASS" if code == 0 else "FAIL"}: {path}', flush=True)
            passed = passed and code == 0
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
