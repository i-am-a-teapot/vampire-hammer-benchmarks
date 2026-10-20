"""XML dependency pins, wrappers and isolated project preparation."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('xml_prover_preparation', HERE.parents[1] / 'scripts/prepare-provers.py')
preparation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preparation)


class ProverDefinitionTests(unittest.TestCase):
    def test_default_environment_covers_the_standard_experiment_without_case_options(self):
        backends = preparation.load_definition(preparation.DEFAULT_DEFINITION)
        normal = ET.parse(preparation.DEFAULT_DEFINITION.with_name('normal.xml')).getroot()
        provers = {run.find('option[@name="--prover"]').text for run in normal.findall('rundefinition')}
        self.assertEqual(set(backends), provers)
        self.assertEqual(len(backends), 6)
        for backend in backends.values():
            self.assertNotIn('case_options', backend)

    def definition(self, root, body='', attributes='name="custom" checkout="builds/custom"'):
        path = root / 'prover.xml'
        path.write_text(f'<prover {attributes}>' + body +
                        '<wrapper><![CDATA[fun _ => `(term| by intros; simp_all)]]></wrapper></prover>')
        return path

    def test_single_prover_defines_checkout_wrapper_and_pinned_dependencies(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            definition = self.definition(root,
                '<dependency name="solver" git="https://example.com/solver.git" commit="' + 'a' * 40 + '"/>'
                '<imports>import Solver</imports><declarations>def helper : Nat := 42</declarations>')
            output = root / 'environment'
            registry = json.loads(preparation.prepare(output, definition=definition).read_text())
            backend = registry['backends']['custom']
            project = output / backend['project']
            self.assertEqual(project, output / 'builds/custom')
            self.assertIn('require solver from git "https://example.com/solver.git" @ "' + 'a' * 40 + '"',
                          (project / 'lakefile.lean').read_text())
            adapter = (project / 'Benchmark/Prover.lean').read_text()
            self.assertIn('initialize registerBenchmarkTactic "custom" fun _ =>', adapter)
            self.assertIn('import Solver', adapter)
            self.assertIn('def helper : Nat := 42', adapter)
            self.assertNotIn('case_options', backend)
            self.assertEqual(backend['definition_sha256'], hashlib.sha256(definition.read_bytes()).hexdigest())
            self.assertEqual(set(preparation.load_definition(output / 'environment.xml')), {'custom'})
            preparation.prepare(output, definition=definition)

    def matching_manifest(self, project, definition):
        backend, = preparation.load_definition(definition).values()
        shared = preparation.shared_dependencies()
        packages = [{'name': 'vampire_hammer_benchmark', 'type': 'path',
                     'dir': os.path.relpath(preparation.PROJECT, project)}]
        for name, dependency in shared.items():
            if dependency['url'] and dependency['revision']:
                packages.append({'name': name, 'type': 'git', 'url': dependency['url'],
                                 'rev': dependency['revision']})
            else:
                packages.append({'name': name, 'type': 'path',
                                 'dir': os.path.relpath(dependency['project'], project)})
        for dependency in backend['dependencies']:
            if dependency['name'] not in shared:
                packages.append({'name': dependency['name'], 'type': 'git',
                                 'url': dependency['git'], 'rev': dependency['commit']})
        (project / 'lake-manifest.json').write_text(json.dumps({'packages': packages}))

    def test_rerun_updates_wrapper_and_preserves_builds_and_unchanged_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            definition = self.definition(root)
            output = root / 'environment'
            preparation.prepare(output, definition=definition)
            project = output / 'builds/custom'
            cached = project / '.lake/build/partial.o'
            cached.parent.mkdir(parents=True)
            cached.write_bytes(b'expensive partial native build')
            lakefile = project / 'lakefile.lean'
            previous_time = lakefile.stat().st_mtime_ns
            self.matching_manifest(project, definition)
            definition.write_text(definition.read_text().replace('simp_all', 'rfl'))
            with patch.object(preparation.subprocess, 'run') as execute:
                preparation.prepare(output, build=True, definition=definition)
            self.assertNotIn(['lake', 'update'], [call.args[0] for call in execute.call_args_list])
            self.assertIn(['lake', 'build'], [call.args[0] for call in execute.call_args_list])
            self.assertIn('by intros; rfl', (project / 'Benchmark/Prover.lean').read_text())
            self.assertEqual(lakefile.stat().st_mtime_ns, previous_time)
            self.assertEqual(cached.read_bytes(), b'expensive partial native build')
            preparation.prepare(output, definition=output / 'environment.xml')
            self.assertEqual(cached.read_bytes(), b'expensive partial native build')

    def test_resume_after_interrupted_build_reuses_the_lockfile(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            definition = self.definition(root)
            output = root / 'environment'
            project = output / 'builds/custom'
            def interrupted(command, **kwargs):
                if command == ['lake', 'update']:
                    self.matching_manifest(project, definition)
                elif command == ['lake', 'build']:
                    raise subprocess.CalledProcessError(1, command)
            with patch.object(preparation.subprocess, 'run', side_effect=interrupted):
                with self.assertRaises(subprocess.CalledProcessError):
                    preparation.prepare(output, build=True, definition=definition)
            with patch.object(preparation.subprocess, 'run') as execute:
                preparation.prepare(output, build=True, definition=definition)
            self.assertEqual([call.args[0] for call in execute.call_args_list], [
                ['lake', 'exe', 'cache', 'get'], ['lake', 'build'], ['lake', 'lean', 'Smoke.lean']])

    def test_source_build_disables_explicit_and_implicit_caches_and_limits_jobs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            definition = self.definition(root)
            with patch.object(preparation.subprocess, 'run') as execute:
                preparation.prepare(root / 'environment', build=True, definition=definition,
                                    no_cache=True, build_jobs=2)
            commands = [call.args[0] for call in execute.call_args_list]
            self.assertEqual([command[4] for command in commands], ['update', 'build', 'lean'])
            for call in execute.call_args_list:
                self.assertEqual(call.args[0][:4], ['lake', '--no-cache', '-K', 'vampireJobs=2'])
                self.assertEqual(call.kwargs['env']['MATHLIB_NO_CACHE_ON_UPDATE'], '1')
                self.assertEqual(call.kwargs['env']['LEAN_NUM_THREADS'], '2')

    def test_first_vampire_checkout_error_is_retried_only_after_native_build_completes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            definition = self.definition(root,
                '<dependency name="vampire_hammer" git="https://example.com/hammer" commit="' + 'a' * 40 + '"/>',
                attributes='name="custom" artifacts="vampire"')
            output = root / 'environment'
            project = output / 'custom'
            preparation.prepare(output, definition=definition)
            self.matching_manifest(project, definition)
            native = project / '.lake/packages/vampire_hammer/.lake/vampire'
            attempts = []
            def first_checkout(command, **kwargs):
                if command == ['lake', 'build']:
                    attempts.append(command)
                    if len(attempts) == 1:
                        (native / 'build').mkdir(parents=True)
                        binary = native / 'build/vampire'
                        binary.write_text('#!/bin/sh\nexit 0\n')
                        binary.chmod(0o755)
                        (native / 'built-from').write_text('native-pin dynamic\n')
                        raise subprocess.CalledProcessError(1, command)
            with patch.object(preparation.subprocess, 'run', side_effect=first_checkout):
                preparation.prepare(output, build=True, definition=definition)
            self.assertEqual(len(attempts), 2)
            attempts.clear()
            def genuine_error(command, **kwargs):
                if command == ['lake', 'build']:
                    attempts.append(command)
                    raise subprocess.CalledProcessError(1, command)
            with patch.object(preparation.subprocess, 'run', side_effect=genuine_error):
                with self.assertRaises(subprocess.CalledProcessError):
                    preparation.prepare(output, build=True, definition=definition)
            self.assertEqual(len(attempts), 1)

    def test_changed_pin_refreshes_lockfile_even_after_a_generate_only_rerun(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            definition = self.definition(root, '<dependency name="solver" git="https://example.com/solver" commit="' + 'a' * 40 + '"/>')
            output = root / 'environment'
            preparation.prepare(output, definition=definition)
            project = output / 'builds/custom'
            self.matching_manifest(project, definition)
            definition.write_text(definition.read_text().replace('a' * 40, 'b' * 40))
            preparation.prepare(output, definition=definition)
            with patch.object(preparation.subprocess, 'run') as execute:
                preparation.prepare(output, build=True, definition=definition)
            self.assertEqual(execute.call_args_list[0].args[0], ['lake', 'update'])

    def test_incremental_selection_preserves_unselected_provers(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            preparation.prepare(output, ['simp_all'])
            source = output / 'simp_all/Benchmark/Prover.lean'
            previous_time = source.stat().st_mtime_ns
            preparation.prepare(output, ['grind'])
            registry = json.loads((output / 'backends.json').read_text())
            self.assertEqual(set(registry['backends']), {'simp_all', 'grind'})
            self.assertEqual(set(preparation.load_definition(output / 'environment.xml')), {'simp_all', 'grind'})
            self.assertEqual(source.stat().st_mtime_ns, previous_time)

    def test_partial_project_is_repaired_without_deleting_existing_checkouts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            definition = self.definition(root)
            output = root / 'environment'
            project = output / 'builds/custom'
            cloned = project / '.lake/packages/mathlib'
            cloned.mkdir(parents=True)
            (cloned / 'preserve.txt').write_text('local checkout')
            notes = project / 'notes.txt'
            notes.write_text('unrelated file')
            preparation.prepare(output, definition=definition)
            self.assertTrue(cloned.is_symlink())
            self.assertEqual((project / '.lake/previous-packages/mathlib/preserve.txt').read_text(), 'local checkout')
            self.assertEqual(notes.read_text(), 'unrelated file')
            preparation.prepare(output, definition=definition)
            self.assertFalse((project / '.lake/previous-packages/mathlib.1').exists())

    def test_invalid_definitions_fail_before_creating_projects(self):
        invalid = [
            '<prover name="p"><wrapper/></prover>',
            '<prover name="p" checkout="../escape"><wrapper>fun _ => pure default</wrapper></prover>',
            '<prover name="p" checkout="/absolute"><wrapper>fun _ => pure default</wrapper></prover>',
            '<prover name="p" checkout="definitions/p"><wrapper>fun _ => pure default</wrapper></prover>',
            '<prover name="p"><dependency name="solver" git="url" commit="main"/>'
            '<wrapper>fun _ => pure default</wrapper></prover>',
            '<prover name="p"><case-option name="maxHeartbeats">0</case-option>'
            '<wrapper>fun _ => pure default</wrapper></prover>',
            '<prover-environment version="2"/>',
            '<prover-environment version="1"/>',
            '<prover-environment version="1"><prover file="missing.xml"/></prover-environment>',
            '<prover',
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            definition = root / 'invalid.xml'
            for source in invalid:
                with self.subTest(source=source):
                    definition.write_text(source)
                    with self.assertRaises(ValueError):
                        preparation.prepare(root / 'output', definition=definition)
                    self.assertFalse((root / 'output').exists())

    def test_environment_rejects_duplicate_names_and_overlapping_checkouts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = self.definition(root)
            second = root / 'second.xml'
            index = root / 'environment.xml'
            index.write_text('<prover-environment version="1"><prover file="prover.xml"/>'
                             '<prover file="second.xml"/></prover-environment>')
            for attrs in ('name="custom" checkout="other"', 'name="other" checkout="builds"'):
                second.write_text(first.read_text().replace('name="custom" checkout="builds/custom"', attrs))
                with self.assertRaisesRegex(ValueError, 'duplicate|overlapping'):
                    preparation.load_definition(index)

    def test_local_dependency_requires_a_matching_clean_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout = root / 'source'
            checkout.mkdir()
            (checkout / 'lakefile.lean').write_text('import Lake\nopen Lake DSL\npackage solver\n')
            subprocess.run(['git', 'init', '-q', str(checkout)], check=True)
            subprocess.run(['git', '-C', str(checkout), 'add', 'lakefile.lean'], check=True)
            subprocess.run(['git', '-C', str(checkout), '-c', 'user.name=Test', '-c',
                            'user.email=test@example.com', '-c', 'commit.gpgsign=false', 'commit', '-qm', 'fixture'], check=True)
            commit = subprocess.check_output(['git', '-C', str(checkout), 'rev-parse', 'HEAD'], text=True).strip()
            definition = self.definition(root,
                f'<dependency name="solver" git="https://example.com/solver" commit="{commit}" path="source"/>')
            registry = preparation.prepare(root / 'environment', definition=definition)
            project = registry.parent / 'builds/custom'
            self.assertIn('require solver from "../../../source"', (project / 'lakefile.lean').read_text())
            saved = preparation.load_definition(registry.parent / 'environment.xml')['custom']
            self.assertEqual(saved['dependencies'][0]['path'], str(checkout))
            definition.write_text(definition.read_text().replace(commit, '0' * 40))
            with self.assertRaisesRegex(ValueError, 'does not match commit'):
                preparation.prepare(root / 'mismatch', definition=definition)
            self.assertFalse((root / 'mismatch').exists())
            definition.write_text(definition.read_text().replace('0' * 40, commit))
            (checkout / 'lakefile.lean').write_text('changed')
            with self.assertRaisesRegex(ValueError, 'modified tracked files'):
                preparation.prepare(root / 'dirty', definition=definition)
            self.assertFalse((root / 'dirty').exists())

    def test_custom_prover_selection_and_shared_pins_are_checked(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            definition = self.definition(root)
            with self.assertRaisesRegex(ValueError, 'selection'):
                preparation.prepare(root / 'unknown', ['not-defined'], definition=definition)
            self.assertFalse((root / 'unknown').exists())
            definition = self.definition(root,
                '<dependency name="mathlib" git="https://example.com/other-mathlib" commit="' + 'a' * 40 + '"/>')
            with self.assertRaisesRegex(ValueError, 'shared Mathlib dependency pin'):
                preparation.prepare(root / 'override', definition=definition)
            self.assertFalse((root / 'override').exists())


if __name__ == '__main__':
    unittest.main()
