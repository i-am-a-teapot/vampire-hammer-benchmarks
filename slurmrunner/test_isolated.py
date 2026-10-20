"""Separate dependency graphs, shared replay cases and shared Slurm scheduling."""
import importlib.machinery
import importlib.util
import json
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(HERE / path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


preparation = load('prepare_isolated_test', 'prepare-provers.py')
suite = load('suite_isolated_test', 'make-suite.py')
runner = load('runner_isolated_test', 'run-hammer-task')
slurm = load('slurm_isolated_test', 'slurmbench.py')


class IsolatedTests(unittest.TestCase):
    def test_projects_do_not_force_other_provers_or_their_auto_versions(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = preparation.prepare(directory, list(preparation.BACKENDS))
            registry = suite.isolated.load_backends(path)
            self.assertEqual(set(registry), set(preparation.BACKENDS))
            for name in registry:
                lakefile = (directory / name / 'lakefile.lean').read_text()
                self.assertIn('require vampire_hammer_benchmark from', lakefile)
                if name != 'vampire-hammer':
                    self.assertNotIn('require vampire_hammer from', lakefile)
                if name not in {'duper', 'lean-auto-duper'}:
                    self.assertNotIn('require Duper from', lakefile)
                if name in {'duper', 'vampire-hammer'}:
                    self.assertNotIn('require auto from', lakefile)
                adapter = (directory / name / 'Benchmark/Prover.lean').read_text()
                self.assertIn('import VampireHammerBenchmark.Comparison', adapter)
                smoke = (directory / name / 'Smoke.lean').read_text()
                self.assertIn(f'for prover in #["{name}"]', smoke)
                self.assertIn('options.set `benchmark.adapterModule "Benchmark.Prover"', smoke)
            original = (directory / 'duper/lakefile.lean').read_bytes()
            with self.assertRaisesRegex(ValueError, 'already exists'):
                preparation.prepare(directory, ['grind', 'duper'])
            self.assertEqual(original, (directory / 'duper/lakefile.lean').read_bytes())

    def test_all_provers_use_the_same_build_sequence(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(preparation.subprocess, 'run') as run:
            preparation.prepare(Path(temporary), ['vampire-hammer', 'duper'], build=True)
            commands = [call.args[0] for call in run.call_args_list]
            expected = [['lake', 'update'], ['lake', 'exe', 'cache', 'get'],
                        ['lake', 'build'], ['lake', 'lean', 'Smoke.lean']]
            self.assertEqual(commands, expected + expected)
            self.assertNotEqual(run.call_args_list[0].kwargs['cwd'], run.call_args_list[4].kwargs['cwd'])

    def test_single_and_batched_cases_load_selected_adapter_without_foreign_options(self):
        for prover in preparation.BACKENDS:
            with self.subTest(prover=prover), tempfile.TemporaryDirectory() as temporary:
                source, _ = runner.write_batch_case(
                    Path(temporary), ['M.first', 'M.second'], 'wrapper', 'parser', 10, '', prover,
                    'import Mathlib\nimport Benchmark.Prover\n', True, 'M', 'Benchmark.Prover')
                text = source.read_text()
                self.assertIn('set_option benchmark.adapterModule "Benchmark.Prover"', text)
                self.assertIn('import Benchmark.Prover', text)
                self.assertIn('hammer_mathlib_try_batch_in_module "M.first M.second"', text)
                if prover != 'vampire-hammer':
                    self.assertNotIn('set_option vampire.timeLimit', text)
                    self.assertNotIn('set_option vampire.executable', text)
                    self.assertNotIn('set_option vampire.outputPath', text)

    def test_registry_suite_uses_one_corpus_and_per_prover_configs_in_slurm(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = preparation.prepare(root / 'provers', ['duper', 'vampire-hammer', 'grind'])
            catalog = root / 'catalog.jsonl'
            catalog.write_text(json.dumps({'name': 'Nat.example', 'module': 'Mathlib.Data.Nat.Basic',
                                           'catalog_strategy': 'lean-auto-style'}) + '\n')

            def configured(name, spec, args, namespace):
                return {'project': spec['project'], 'provers': [name], 'lean_version': 'Lean same',
                        'mathlib_commit': 'mathlib-same', 'backend_source_hashes': {'lakefile.lean': 'hash'}}

            output = root / 'suite'
            with patch.object(suite.isolated, 'backend_config', side_effect=configured), \
                    patch.object(sys, 'argv', ['make-suite.py', '--backends', str(registry),
                                              '--comparison', '--compact', '--catalog', str(catalog),
                                              '--output', str(output)]):
                self.assertEqual(suite.main(), 0)
            manifest = slurm.parse_xml(output / 'vampire-hammer.xml')
            self.assertEqual(len(manifest['jobs']), 3)
            self.assertEqual(len({job['input'] for job in manifest['jobs']}), 1)
            configs = set()
            for job in manifest['jobs']:
                args = job['args']
                prover = args[args.index('--prover') + 1]
                self.assertEqual(args.count('--config'), 1)
                config = Path(args[args.index('--config') + 1])
                self.assertEqual(config.name, f'{prover}-config.json')
                configs.add(config)
                self.assertEqual(json.loads(config.read_text())['provers'], [prover])
            self.assertEqual(len(configs), 3)
            self.assertEqual(len(slurm.batches(manifest, 16)), 3)

    def test_configuring_a_backend_restores_shared_context_on_error(self):
        before = suite.PROJECT, suite.LAKE, suite.benchmark.PROJECT, suite.benchmark.LAKE
        with self.assertRaisesRegex(RuntimeError, 'probe failed'):
            with suite.isolated.project_context(suite.__dict__, Path('/isolated')):
                self.assertEqual(suite.PROJECT, Path('/isolated'))
                raise RuntimeError('probe failed')
        self.assertEqual(before, (suite.PROJECT, suite.LAKE, suite.benchmark.PROJECT, suite.benchmark.LAKE))

    def test_runner_does_not_inherit_foreign_lean_module_paths(self):
        with patch.dict(os.environ, {'LEAN_PATH': '/foreign/auto', 'LEAN_SRC_PATH': '/foreign/source'}):
            command, environment = runner.lean_command(
                {'lean': '/selected/lean', 'isolated_backend': True,
                 'lean_env': {'LEAN_PATH': '/selected/modules'}}, Path('/case.lean'))
        self.assertIn('/selected/lean', command)
        self.assertEqual(environment['LEAN_PATH'], '/selected/modules')
        self.assertNotIn('LEAN_SRC_PATH', environment)

    def test_stale_backend_sources_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'Prover.lean'
            source.write_text('original')
            config = {'isolated_backend': True, 'project': str(root),
                      'backend_source_hashes': {'Prover.lean': hashlib.sha256(source.read_bytes()).hexdigest()}}
            runner.validate_isolated_config(config)
            source.write_text('changed')
            with self.assertRaisesRegex(ValueError, 'sources changed'):
                runner.validate_isolated_config(config)

    def test_subset_comparison_uses_only_selected_provers(self):
        with tempfile.TemporaryDirectory() as temporary:
            completed = []
            for prover in ['duper', 'grind']:
                job = {'input': 'A', 'args': ['--prover', prover, '--search-seconds', '10']}
                completed.append((job, {'status': 'true', 'extracted': {'outcome': 'proved'}}, None))
            slurm.comparison_diagnostics(Path(temporary), completed, ['duper', 'grind'])
            report = json.loads((Path(temporary) / 'comparison-search10.json').read_text())
            self.assertEqual(report['provers'], ['duper', 'grind'])
            self.assertEqual(report['common_eligible'], 1)

    def test_local_execution_uses_the_common_worker_and_can_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / 'fake-prover'
            counter = root / 'calls.txt'
            executable.write_text('#!' + sys.executable + '\n'
                                  'from pathlib import Path\n'
                                  f'p = Path({str(counter)!r})\n'
                                  'p.write_text(p.read_text() + "x" if p.exists() else "x")\n'
                                  'print(\'HAMMER-RESULT: {"name":"A", "status":"proved", "seconds":0.1}\')\n')
            executable.chmod(0o755)
            (root / 'A.thm').write_text('A\nM\n')
            xml = root / 'benchmark.xml'
            xml.write_text(f'<benchmark command="{executable}" tool="hammer_bench.tool_info" timelimit="10 s">'
                           '<rundefinition name="duper-search10"><option name="--prover">duper</option>'
                           '<option name="--search-seconds">10</option></rundefinition>'
                           '<tasks name="all"><include>A.thm</include></tasks></benchmark>')
            args = types.SimpleNamespace(jobs=1, batch_by_module=1, output=str(root / 'results'),
                                         benchmark=str(xml), resume=False)
            slurm.run_local(args)
            self.assertEqual(json.loads((root / 'results/logs/000000.json').read_text())['status'], 'true')
            args.resume = True
            slurm.run_local(args)
            self.assertEqual(counter.read_text(), 'x')


if __name__ == '__main__':
    unittest.main()
