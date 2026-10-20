"""Separate dependency graphs, shared replay cases and shared Slurm scheduling."""
import importlib.machinery
import importlib.util
import json
import hashlib
import os
import shutil
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
relocation = load('relativize_isolated_test', '../scripts/relativize-provers.py')


class IsolatedTests(unittest.TestCase):
    def test_existing_compiled_projects_can_be_made_relative(self):
        with tempfile.TemporaryDirectory() as temporary:
            core = Path(temporary) / 'original'
            project = core / 'artifacts/provers/duper'
            packages = project / '.lake/packages'
            packages.mkdir(parents=True)
            mathlib = core / '.lake/packages/mathlib'
            mathlib.mkdir(parents=True)
            (packages / 'mathlib').symlink_to(mathlib, target_is_directory=True)
            (project / 'lakefile.lean').write_text(f'require core from {json.dumps(str(core))}\n')
            (project / 'lake-manifest.json').write_text(json.dumps({'packages': [
                {'name': 'core', 'type': 'path', 'dir': str(core)}]}))
            binary = project / '.lake/build/lib/adapter.so'
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b'compiled artifact')
            registry = project.parent / 'backends.json'
            registry.write_text(json.dumps({'version': 1, 'backends': {
                'duper': {'project': str(project), 'shared_dependencies': {
                    'mathlib': {'project': str(mathlib)}}}}}))
            relocation.relativize(core)
            first = registry.read_bytes()
            relocation.relativize(core)
            self.assertEqual(first, registry.read_bytes())
            moved = core.with_name('moved')
            core.rename(moved)
            spec = suite.isolated.load_backends(moved / 'artifacts/provers/backends.json')['duper']
            project = Path(spec['project'])
            self.assertEqual((project / '.lake/packages/mathlib').resolve(), moved / '.lake/packages/mathlib')
            self.assertEqual(json.loads((project / 'lake-manifest.json').read_text())['packages'][0]['dir'], '../../..')
            self.assertEqual((project / '.lake/build/lib/adapter.so').read_bytes(), b'compiled artifact')

    def test_prepared_paths_survive_moving_the_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            core = base / 'original'
            mathlib = core / '.lake/packages/mathlib'
            mathlib.mkdir(parents=True)
            (core / 'lean-toolchain').write_text('leanprover/lean4:v4.34.1\n')
            (core / 'lake-manifest.json').write_text(json.dumps({'packages': [
                {'name': 'mathlib', 'type': 'git', 'rev': 'pin', 'url': 'https://example.com/mathlib'}]}))
            fixture = core / 'slurmrunner/tests/ComparisonBenchmarkTests.lean'
            fixture.parent.mkdir(parents=True)
            fixture.write_text((HERE / 'tests/ComparisonBenchmarkTests.lean').read_text())
            with patch.object(preparation, 'PROJECT', core):
                registry = preparation.prepare(core / 'artifacts/provers', ['duper'])
            raw = json.loads(registry.read_text())['backends']['duper']
            self.assertFalse(Path(raw['project']).is_absolute())
            self.assertFalse(Path(raw['shared_dependencies']['mathlib']['project']).is_absolute())
            link = core / 'artifacts/provers/duper/.lake/packages/mathlib'
            self.assertFalse(Path(os.readlink(link)).is_absolute())
            self.assertNotIn(str(core), (core / 'artifacts/provers/duper/lakefile.lean').read_text())
            moved = base / 'moved'
            core.rename(moved)
            spec = suite.isolated.load_backends(moved / 'artifacts/provers/backends.json')['duper']
            self.assertEqual(Path(spec['project']), moved / 'artifacts/provers/duper')
            self.assertEqual(Path(spec['shared_dependencies']['mathlib']['project']),
                             moved / '.lake/packages/mathlib')
            self.assertEqual((moved / 'artifacts/provers/duper/.lake/packages/mathlib').resolve(),
                             moved / '.lake/packages/mathlib')

    @unittest.skipUnless(shutil.which('rsync'), 'rsync required for node-local staging')
    def test_scratch_staging_copies_shared_mathlib_only_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            core = base / 'core'
            mathlib = core / '.lake/packages/mathlib'
            toolchain = base / 'toolchain'
            scratch = base / 'scratch'
            scratch.mkdir()
            for path in (mathlib / 'Mathlib.olean', mathlib / 'libMathlib.so',
                         core / 'slurmrunner/run-hammer-task', toolchain / 'bin/lean'):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(path.name)
            jobs = []
            for name in ('one', 'two'):
                project = core / 'artifacts' / name
                packages = project / '.lake/packages'
                packages.mkdir(parents=True)
                (packages / 'mathlib').symlink_to(mathlib, target_is_directory=True)
                (packages / 'auto').mkdir()
                (packages / 'auto/version').write_text(name)
                config = {'project': str(project), 'shared_core_project': str(core),
                          'lean': str(toolchain / 'bin/lean'), 'lean_env': {},
                          'lean_args': [f'--plugin={packages / "mathlib/libMathlib.so"}'],
                          'shared_dependencies': {'mathlib': {'project': str(mathlib), 'revision': 'pin'}}}
                path = base / f'{name}.json'
                path.write_text(json.dumps(config))
                jobs.append({'args': ['--config', str(path)]})
            config = {'tool': 'hammer_bench.tool_info', 'jobs': jobs, 'workdir': str(base),
                      'executable': str(core / 'slurmrunner/run-hammer-task')}
            staged, directory = slurm.stage_hammer(config, scratch)
            self.assertEqual(len(list(directory.rglob('Mathlib.olean'))), 1)
            configs = [json.loads(Path(job['args'][1]).read_text()) for job in staged['jobs']]
            self.assertEqual(configs[0]['lean_args'], configs[1]['lean_args'])
            shutil.rmtree(core)
            shutil.rmtree(toolchain)
            for name, local in zip(('one', 'two'), configs):
                packages = Path(local['project']) / '.lake/packages'
                self.assertTrue((packages / 'mathlib').is_symlink())
                self.assertTrue((packages / 'mathlib').resolve().is_relative_to(directory))
                self.assertEqual((packages / 'mathlib/Mathlib.olean').read_text(), 'Mathlib.olean')
                self.assertEqual((packages / 'auto/version').read_text(), name)

    def test_projects_do_not_force_other_provers_or_their_auto_versions(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = preparation.prepare(directory, list(preparation.BACKENDS))
            registry = suite.isolated.load_backends(path)
            self.assertEqual(set(registry), set(preparation.BACKENDS))
            self.assertNotIn('lean-smt', registry)
            self.assertTrue(all('cvc5' not in spec['requires'] for spec in preparation.BACKENDS.values()))
            shared = preparation.shared_dependencies()
            for name in registry:
                lakefile = (directory / name / 'lakefile.lean').read_text()
                self.assertEqual(registry[name]['shared_dependencies'], shared)
                for package, dependency in shared.items():
                    link = directory / name / '.lake/packages' / package
                    self.assertTrue(link.is_symlink())
                    self.assertEqual(link.resolve(), Path(dependency['project']))
                    self.assertIn(f'require {package} from git {json.dumps(dependency["url"])} '
                                  f'@ {json.dumps(dependency["revision"])}', lakefile)
                self.assertIn('require vampire_hammer_benchmark from', lakefile)
                self.assertIn('precompileModules := true', lakefile)
                self.assertIn('lean_lib Benchmark.Prover where', lakefile)
                self.assertIn('defaultFacets := #[LeanLib.sharedFacet]', lakefile)
                if name != 'vampire-hammer':
                    self.assertNotIn('require vampire_hammer from', lakefile)
                if name not in {'duper', 'lean-auto-duper'}:
                    self.assertNotIn('require Duper from', lakefile)
                if name in {'duper', 'vampire-hammer'}:
                    self.assertNotIn('require auto from', lakefile)
                adapter = (directory / name / 'Benchmark/Prover.lean').read_text()
                self.assertIn('import Mathlib.Init', adapter)
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

    def test_shared_dependency_path_and_revision_must_match(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            shared = project / 'baseline'
            shared.mkdir()
            (project / 'lake-manifest.json').write_text(json.dumps({'packages': [
                {'name': 'mathlib', 'type': 'path', 'dir': str(shared)}]}))
            spec = {'project': str(project), 'shared_dependencies': {
                'mathlib': {'project': str(shared), 'revision': 'pinned'}}}
            with patch.object(suite.isolated.subprocess, 'check_output', return_value='changed\n'), \
                    self.assertRaisesRegex(ValueError, 'changed revision'):
                suite.isolated.backend_config('simp_all', spec, None, suite.__dict__)
            spec['shared_dependencies']['mathlib']['project'] = str(project / 'other')
            with self.assertRaisesRegex(ValueError, 'must use shared dependency'):
                suite.isolated.backend_config('simp_all', spec, None, suite.__dict__)

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
