import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    'docker_entrypoint', Path(__file__).resolve().parents[2] / 'docker/entrypoint.py')
entrypoint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entrypoint)


class DockerEntrypointTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.definition = self.root / 'experiment.xml'
        self.definition.write_text('<benchmark><tasks/></benchmark>')
        self.output = self.root / 'run'
        self.environment = patch.dict(os.environ, {
            'BENCHMARK_ROOT': str(self.root),
            'BENCHMARK_DEFINITION': str(self.definition),
            'BENCHMARK_COUNT': '7', 'BENCHMARK_JOBS': '2',
            'BENCHMARK_OUTPUT': str(self.output),
            'BENCHMARK_CATALOG': str(self.root / 'catalog.jsonl'),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_exports_selected_sample_and_replaces_process_with_benchexec(self):
        with patch.object(entrypoint.subprocess, 'run') as generate, \
                patch.object(entrypoint.os, 'execvpe') as execute:
            generate.return_value.returncode = 0
            entrypoint.main(['--count', '3', '--jobs', '1', '--prover', 'grind', '--seed', 'pilot'])
        arguments = generate.call_args.args[0]
        self.assertEqual(arguments[arguments.index('--count') + 1], '3')
        self.assertEqual(arguments[arguments.index('--format') + 1], 'benchexec')
        self.assertEqual(arguments[arguments.index('--definition') + 1], str(self.definition))
        self.assertEqual(arguments[arguments.index('--prover') + 1], 'grind')
        self.assertEqual(arguments[arguments.index('--seed') + 1], 'pilot')
        binary, command, environment = execute.call_args.args
        self.assertEqual(binary, 'benchexec')
        self.assertIn('--no-container', command)
        self.assertEqual(command[command.index('--numOfThreads') + 1], '1')
        self.assertEqual(command[-1], str(self.output / 'suite/vampire-hammer.xml'))
        self.assertEqual(environment['PYTHONPATH'].split(os.pathsep)[0], str(self.root / 'scripts'))
        self.assertEqual(json.loads((self.output / 'benchexec-command.json').read_text()), command)

    def test_xml_task_source_is_preserved_and_extract_only_does_not_execute(self):
        for option in ('--catalog', '--test-suite'):
            with self.subTest(option=option):
                self.definition.write_text(
                    f'<benchmark><tasks><option name="{option}">cases</option></tasks></benchmark>')
                with patch.object(entrypoint.subprocess, 'run') as generate, \
                        patch.object(entrypoint.os, 'execvpe') as execute:
                    generate.return_value.returncode = 0
                    self.assertEqual(entrypoint.main(['--extract-only']), 0)
                self.assertNotIn('--catalog', generate.call_args.args[0])
                self.assertEqual(generate.call_args.args[0][generate.call_args.args[0].index('--count') + 1], '7')
                execute.assert_not_called()
                self.output.rmdir()

    def test_generation_failure_propagates_without_running_benchexec(self):
        with patch.object(entrypoint.subprocess, 'run') as generate, \
                patch.object(entrypoint.os, 'execvpe') as execute:
            generate.return_value.returncode = 4
            self.assertEqual(entrypoint.main([]), 4)
        execute.assert_not_called()

    def test_rejects_nonpositive_sample_and_existing_output_before_generation(self):
        for arguments in (['--count', '0'], ['--count', '-3'], ['--jobs', '0']):
            with self.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()), \
                    patch.object(entrypoint.subprocess, 'run') as generate:
                with self.assertRaises(SystemExit) as error:
                    entrypoint.main(arguments)
                self.assertEqual(error.exception.code, 2)
                generate.assert_not_called()
        self.output.mkdir()
        with contextlib.redirect_stderr(io.StringIO()), patch.object(entrypoint.subprocess, 'run') as generate:
            with self.assertRaises(SystemExit):
                entrypoint.main([])
            generate.assert_not_called()
