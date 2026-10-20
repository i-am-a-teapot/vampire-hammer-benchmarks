import contextlib
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

REPOSITORY = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('docker_bootstrap', REPOSITORY / 'docker/bootstrap.py')
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class DockerBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.source = self.directory / 'sources'
        self.source.mkdir()
        (self.source / 'lean-toolchain').write_text('leanprover/lean4:v4.34.1\n')
        self.definition = self.source / 'provers.xml'
        self.definition.write_text('<prover-environment version="1"><prover file="prover.xml"/></prover-environment>')
        (self.source / 'prover.xml').write_text('<prover name="grind"><wrapper>tactic</wrapper></prover>')
        (self.source / 'docker').mkdir()
        shutil.copy2(REPOSITORY / 'docker/entrypoint.py', self.source / 'docker/entrypoint.py')
        self.state = self.directory / 'state'

    def fake_build(self, command, cwd, **kwargs):
        root = Path(cwd)
        if 'scripts/prepare-provers.py' in command:
            (root / 'artifacts/provers').mkdir(parents=True, exist_ok=True)
            (root / 'artifacts/provers/backends.json').write_text('{"version": 1, "backends": {}}')
        elif 'lean' in command:
            (root / 'artifacts/catalog.jsonl').write_text('{}\n')

    def test_fresh_build_uses_locked_sources_without_caches_then_reuses_volume(self):
        with patch.object(bootstrap.subprocess, 'run', side_effect=self.fake_build) as execute:
            root, catalog = bootstrap.prepare(self.source, self.state, self.definition, 2)
            self.assertTrue(catalog.is_file())
            self.assertEqual(execute.call_args_list[0].args[0],
                             ['lake', '--no-cache', 'build', 'Mathlib:shared', 'VampireHammerBenchmark'])
            self.assertIn('--no-cache', execute.call_args_list[1].args[0])
            self.assertIn('--build-jobs', execute.call_args_list[1].args[0])
            for call in execute.call_args_list:
                self.assertEqual(call.kwargs['env']['MATHLIB_NO_CACHE_ON_UPDATE'], '1')
            before = (root / 'lean-toolchain').stat().st_mtime_ns
            execute.reset_mock()
            self.assertEqual(bootstrap.prepare(self.source, self.state, self.definition, 4), (root, catalog))
            execute.assert_not_called()
            self.assertEqual((root / 'lean-toolchain').stat().st_mtime_ns, before)

    def test_interrupted_build_has_no_success_stamp_and_resumes_existing_artifacts(self):
        def interrupted(command, cwd, **kwargs):
            (Path(cwd) / 'partial.o').write_bytes(b'expensive compilation')
            raise subprocess.CalledProcessError(1, command)
        with patch.object(bootstrap.subprocess, 'run', side_effect=interrupted):
            with self.assertRaises(subprocess.CalledProcessError):
                bootstrap.prepare(self.source, self.state, self.definition, 2)
        self.assertEqual(list(self.state.rglob('.prepared.json')), [])
        with patch.object(bootstrap.subprocess, 'run', side_effect=self.fake_build):
            root, _ = bootstrap.prepare(self.source, self.state, self.definition, 2)
        self.assertEqual((root / 'partial.o').read_bytes(), b'expensive compilation')
        self.assertTrue((root / '.prepared.json').is_file())

    def test_changed_mounted_prover_definition_uses_another_workspace(self):
        definition = self.directory / 'mounted.xml'
        definition.write_text('<prover name="grind"><wrapper>first</wrapper></prover>')
        first = bootstrap.fingerprint(self.source, definition)
        definition.write_text('<prover name="grind"><wrapper>second</wrapper></prover>')
        self.assertNotEqual(bootstrap.fingerprint(self.source, definition), first)
        # An index also tracks changes to its referenced definitions.
        first = bootstrap.fingerprint(self.source, self.definition)
        (self.source / 'prover.xml').write_text('<prover name="grind"><wrapper>changed</wrapper></prover>')
        self.assertNotEqual(bootstrap.fingerprint(self.source, self.definition), first)

    def test_main_executes_sample_with_volume_paths_and_holds_volume_lock(self):
        environment = {'BENCHMARK_SOURCE': str(self.source), 'BENCHMARK_STATE': str(self.state),
                       'BENCHMARK_PROVERS': 'provers.xml', 'BENCHMARK_DEFINITION': 'experiment.xml'}
        root = self.state / 'workspaces/compiled'
        root.mkdir(parents=True)
        def verify_lock(*args):
            with (self.state / '.lock').open('a') as other:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with patch.dict(os.environ, environment), \
                patch.object(bootstrap, 'prepare', return_value=(root, root / 'catalog.jsonl')), \
                patch.object(bootstrap.os, 'chdir'), \
                patch.object(bootstrap.os, 'execvpe', side_effect=verify_lock) as execute:
            bootstrap.main(['--count', '12', '--jobs', '3'])
        _, command, runtime = execute.call_args.args
        self.assertEqual(command[-4:], ['--count', '12', '--jobs', '3'])
        self.assertEqual(runtime['BENCHMARK_ROOT'], str(root))
        self.assertEqual(runtime['BENCHMARK_DEFINITION'], str(root / 'experiment.xml'))
        self.assertEqual(runtime['ELAN_HOME'], str(self.state / 'elan'))

    def test_invalid_sample_options_fail_before_any_build(self):
        with patch.dict(os.environ, {'BENCHMARK_SOURCE': str(self.source)}), \
                patch.object(bootstrap, 'prepare') as prepare, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                bootstrap.main(['--count', '0'])
            prepare.assert_not_called()
