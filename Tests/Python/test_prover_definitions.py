"""XML dependency pins, wrappers and isolated project preparation."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET

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
            with self.assertRaisesRegex(ValueError, 'already exists'):
                preparation.prepare(output, definition=definition)

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
