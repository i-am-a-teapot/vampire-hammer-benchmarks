"""Declaration replay can locate source using catalog metadata without Mathlib preload."""
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest

HERE = Path(__file__).resolve().parent

def load(name, file):
    loader = importlib.machinery.SourceFileLoader(name, str(HERE / file))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module

runner = load('startup_runner', 'run-hammer-task')
suite = load('startup_suite', 'make-suite.py')
slurm = load('startup_slurm', 'slurmbench.py')

class BatchTests(unittest.TestCase):
    """Several theorems of one module share one source replay."""

    def job(self, theorem, module, prover='vampire-hammer'):
        args = ['--config', 'suite-config.json', '--prover', prover, '--module', module, '--theorem', theorem]
        return {'theorem': theorem, 'args': args}

    def test_batch_case_uses_one_replay_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, _ = runner.write_batch_case(Path(temporary), ['A.one', 'A.two'], 'wrapper', 'parser', 10, '',
                'vampire-hammer', 'import Mathlib\nimport VampireHammerBenchmark.Mathlib\n', False, 'Mathlib.A')
            text = source.read_text()
        self.assertIn('hammer_mathlib_try_batch_in_module "A.one A.two" module "Mathlib.A" recording ', text)
        self.assertNotIn('hammer_mathlib_try_with_premises_in_module', text)

    def test_batches_group_consecutive_jobs_of_one_module_and_tactic(self):
        jobs = [self.job('A.one', 'Mathlib.A'), self.job('A.two', 'Mathlib.A'), self.job('A.three', 'Mathlib.A'),
                self.job('B.one', 'Mathlib.B'), self.job('B.two', 'Mathlib.B', prover='duper'),
                {'theorem': None, 'args': ['task.yml']}]
        self.assertEqual(slurm.batches({'jobs': jobs}, 2), [[0, 1], [2], [3], [4], [5]])

    def test_sections_split_at_headers_and_keep_shared_output_first(self):
        text = ('[run-hammer-task] batch of 2 in Mathlib.A; attempt 1\n[vampire.profile] replay\n'
                '[run-hammer-task] theorem A.one; prover vampire-hammer\nHAMMER-RESULT: {"status": "proved"}\n'
                '[run-hammer-task] theorem A.two; prover vampire-hammer\nHAMMER-RESULT: {"status": "error"}\n')
        sections = slurm.split_sections(text)
        self.assertEqual(list(sections), ['A.one', 'A.two'])
        self.assertTrue(sections['A.one'].startswith('[run-hammer-task] batch of 2'))
        self.assertIn('"proved"', sections['A.one'])
        self.assertNotIn('"proved"', sections['A.two'])
        self.assertEqual(slurm.hammer_metrics(sections['A.two'], 0)['theorem'], 'A.two')

    def test_sections_route_pre_header_diagnostic_events_by_theorem(self):
        first = {'event': 'route_selected', 'name': 'A.one', 'route': 'fo'}
        second = {'event': 'solver_result', 'name': 'A.two', 'szs_status': 'Timeout'}
        text = ('[run-hammer-task] batch of 2 in Mathlib.A; attempt 1\n' +
                '[vampire.event] ' + json.dumps(first) + '\n' +
                '[vampire.event] ' + json.dumps(second) + '\n' +
                '[run-hammer-task] theorem A.one; prover vampire-hammer\n' +
                'HAMMER-RESULT: {"name":"A.one","status":"error"}\n' +
                '[run-hammer-task] theorem A.two; prover vampire-hammer\n' +
                'HAMMER-RESULT: {"name":"A.two","status":"error"}\n')
        sections = slurm.split_sections(text)
        self.assertEqual(slurm.hammer_metrics(sections['A.one'], 0)['attempt-events'], [first])
        self.assertEqual(slurm.hammer_metrics(sections['A.two'], 0)['attempt-events'], [second])

    def test_batch_harness_overrides_diagnostic_case_per_target(self):
        source = (HERE.parent / 'VampireHammerBenchmark/Mathlib.lean').read_text()
        self.assertIn('options.set `vampire.diagnosticCase target.toString', source)


class StartupTests(unittest.TestCase):
    def case(self, strategy='lean-auto-style', module='Mathlib.Data.Int.Order.Basic', comparison=False):
        with tempfile.TemporaryDirectory() as temporary:
            source, _ = runner.write_case(Path(temporary), 'Int.example', 'wrapper', 'parser', 10, '',
                strategy, imports='import Mathlib\nimport VampireHammerBenchmark.Comparison\n',
                comparison=comparison, module=module)
            return source.read_text()

    def test_module_metadata_skips_full_import_but_keeps_comparison(self):
        text = self.case(comparison=True)
        self.assertNotIn('import Mathlib\n', text)
        self.assertIn('import Mathlib.Data.Int.Order.Basic\n', text)
        self.assertIn('import VampireHammerBenchmark.Comparison\n', text)
        self.assertIn('hammer_mathlib_try_with_premises_in_module "Int.example" module "Mathlib.Data.Int.Order.Basic" recording ', text)
        self.assertIn('set_option vampire.benchmarkComparison true', text)

    def test_old_tasks_and_statement_strategy_keep_their_environment(self):
        self.assertIn('import Mathlib\n', self.case(module=None))
        self.assertIn('hammer_mathlib_try_with_premises "Int.example"', self.case(module=None))
        self.assertIn('import Mathlib\n', self.case(strategy='hammer'))
        self.assertIn('hammer_mathlib_try "Int.example"', self.case(strategy='hammer'))

    def test_module_metadata_cannot_inject_lean_commands(self):
        with self.assertRaises(ValueError):
            self.case(module='Mathlib.M\naxiom injected : False')

    def test_regular_tasks_carry_module_to_both_runner_entry_points(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            suite.write_tasks([{'id':0,'name':'Int.example','module':'Mathlib.M'}], directory, 10)
            for suffix in ('thm', 'yml'):
                file = next(directory.rglob('*.' + suffix))
                self.assertEqual(runner.task_theorem(file), 'Int.example')
                self.assertEqual(runner.task_module(file), 'Mathlib.M')
            old = directory/'old.thm';old.write_text('Int.example\n')
            self.assertIsNone(runner.task_module(old))
            old_yaml = directory/'old.yml'
            old_yaml.write_text('options:\n  theorem: "Int.example"\n')
            self.assertIsNone(runner.task_module(old_yaml))

    def test_compact_jobs_keep_module_and_old_catalogs_remain_valid(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            suite.write_tasks([{'id':0,'name':'Int.example','module':'Mathlib.M'}], directory, 10, compact=True)
            args = types.SimpleNamespace(case_option=[], keep_artifacts=False, comparison=True,
                backend_configs={'simp_all': directory / 'simp_all-config.json'},
                compact=True, shard_size=10,search_seconds=[10],timelimit='180 s',memlimit='8 GB',
                cpu_cores=1,hardtimelimit=None,walltimelimit='240 s')
            xml = directory/'benchmark.xml';xml.write_text(suite.benchmark_xml(args,directory/'suite-config.json',{0:1}))
            for job in slurm.parse_xml(xml)['jobs']:
                i=job['args'].index('--module');self.assertEqual(job['args'][i+1],'Mathlib.M')
            file=directory/'theorems.jsonl';rows=[json.loads(line) for line in file.read_text().splitlines()]
            for row in rows:row.pop('module')
            file.write_text(''.join(json.dumps(row)+'\n' for row in rows))
            self.assertTrue(all('--module' not in job['args'] for job in slurm.parse_xml(xml)['jobs']))

if __name__ == '__main__':
    unittest.main()
