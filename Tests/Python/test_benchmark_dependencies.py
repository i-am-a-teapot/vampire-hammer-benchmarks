import csv
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('benchmark_dependencies', ROOT / 'scripts/benchmark-mathlib.py')
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


class DependencyTests(unittest.TestCase):
    def test_local_library_and_transitive_parser_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'benchmark'
            root.mkdir()
            library = root.parent / 'library'
            (root / 'lake-manifest.json').write_text(json.dumps({'packages': [
                {'name': 'vampire_hammer', 'type': 'path', 'dir': '../library'},
                {'name': 'tptp_parser', 'type': 'path', 'dir': '../library/vendor/tptp_parser'},
                {'name': 'mathlib', 'type': 'git'},
            ]}))
            self.assertEqual(benchmark.hammer_project(root), library)
            self.assertEqual(benchmark.vampire_executable(root), library / '.lake/vampire/build/vampire')
            self.assertEqual(benchmark.package_root('tptp_parser', root), library / 'vendor/tptp_parser')
            self.assertEqual(benchmark.package_root('mathlib', root), root / '.lake/packages/mathlib')

    def test_export_keeps_runs_failures_and_original_times_separate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'scope'
            source.mkdir()
            (source / 'comparison.csv').write_text('prover\nsolver\ntheoretical-best\n')
            (source / 'problems-solver.csv').write_text(
                'theorem,run_1_status,run_2_status,run_1_seconds,run_2_seconds\n'
                'A,true,unknown,0.25,10.0\n'
                'B,ERROR (replay),true,,0.5\n')
            subprocess.run([sys.executable, str(ROOT / 'slurmrunner/export-comparison-runs.py'),
                            '--source', str(source), '--output', str(root / 'csv')],
                           check=True, capture_output=True)
            with (root / 'csv/benchmark-run-1.csv').open() as stream:
                first = list(csv.DictReader(stream))
            with (root / 'csv/benchmark-run-2.csv').open() as stream:
                second = list(csv.DictReader(stream))
            self.assertEqual(first[0]['solver_seconds'], '0.25')
            self.assertEqual(second[0]['solver_seconds'], '10.0')
            self.assertEqual(second[0]['solver_status'], 'unknown')
            self.assertEqual(first[1]['solver_status'], 'ERROR (replay)')
            self.assertEqual(first[1]['solver_seconds'], '')
            self.assertEqual(second[1]['solver_seconds'], '0.5')
