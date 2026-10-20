"""Regression checks for comparison denominators and result integrity."""
import importlib.util
from pathlib import Path
import sys
import math
from unittest.mock import patch
import tempfile
import unittest
import xml.etree.ElementTree as ET

spec = importlib.util.spec_from_file_location('plot_comparison', Path(__file__).resolve().parents[2] / 'scripts/plot-comparison.py')
plot = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plot
spec.loader.exec_module(plot)


class ComparisonTests(unittest.TestCase):
    def test_pairwise_excludes_only_the_selected_tools(self):
        result = plot.Result
        data = {
            'hammer': {'a': result('true', 'proved', 1), 'b': result('true', 'proved', 2),
                       'c': result('ERROR (sorry)', 'sorry', .1)},
            'other': {'a': result('true', 'proved', 1), 'b': result('unknown', 'timeout', 10),
                      'c': result('true', 'proved', 1)},
            'smt': {'a': result('unknown', 'excluded', None), 'b': result('true', 'proved', 1),
                    'c': result('unknown', 'timeout', 10)},
        }
        common, _, _ = plot.summarize(data, list(data))
        pair, eligible, _ = plot.summarize(data, ['hammer', 'other'])
        self.assertEqual(common['common_eligible'], 2)
        self.assertEqual(eligible, {'a', 'b', 'c'})
        self.assertEqual(pair['pairwise']['hammer']['other'],
                         dict(both=1, only_first=1, only_second=1))
        self.assertEqual(pair['proved'], {'hammer': 2, 'other': 2})
        del data['other']['b']
        partial, _, _ = plot.summarize(data, ['hammer', 'other'])
        self.assertEqual(partial['incomplete'], 1)
        self.assertEqual(partial['common_eligible'], 2)

    def read_fixture(self, records, run_label=None):
        root = ET.Element('result')
        for name, cols in records:
            run = ET.SubElement(root, 'run', name=f'cases.jsonl#000001-{name}',
                                options='--prover hammer ' +
                                (f'--run-label {run_label} ' if run_label else '') +
                                '--theorem ' + __import__('shlex').quote(name))
            for key, value in dict(prover='hammer', **cols).items():
                ET.SubElement(run, 'column', title=key, value=value)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'results.xml'
            ET.ElementTree(root).write(path)
            return plot.read_results(path, 10)

    def test_named_configuration_identifies_curve_separately_from_prover(self):
        name, rows, _ = self.read_fixture([
            ('T', {'theorem': 'T', 'status': 'unknown', 'outcome': 'timeout'})], run_label='hammer-tuned')
        self.assertEqual(name, 'hammer-tuned')
        self.assertEqual(set(rows), {'T'})

    def test_success_integrity_and_missing_metadata(self):
        _, rows, audit = self.read_fixture([
            ('zero', {'theorem': 'zero', 'status': 'true', 'outcome': 'proved',
                      'kernel-checked': 'True', 'hammer-seconds': '0'}),
            ('late', {'theorem': 'late', 'status': 'true', 'outcome': 'proved',
                      'kernel-checked': 'False', 'hammer-seconds': '10.01'}),
            ('invalid', {'theorem': 'invalid', 'status': 'true', 'outcome': 'proved',
                         'kernel-checked': 'True', 'hammer-seconds': 'nan'}),
            ('crash', {'status': 'ERROR (no_result)', 'outcome': 'no_result'}),
        ])
        self.assertEqual(rows['zero'].seconds, 0)
        self.assertIn('crash', rows)
        self.assertEqual(audit['issues'], dict(success_without_kernel_check=1,
                         success_over_budget=1, invalid_time=1, success_without_time=1))

    def test_scheduled_identity_survives_misattributed_batch_result(self):
        _, rows, audit = self.read_fixture([
            ("scheduled'", {'theorem': 'another', 'status': 'unknown', 'outcome': 'timeout'}),
            ('another', {'theorem': 'another', 'status': 'ERROR (no_result)', 'outcome': 'no_result'}),
        ])
        self.assertEqual(set(rows), {"scheduled'", 'another'})
        self.assertEqual(audit['metadata']['theorem_mismatch'], 1)
        self.assertEqual(audit['error_examples']['theorem_mismatch'][0]['reported_theorem'], 'another')

    def test_duplicate_repetition_is_rejected(self):
        cols = {'theorem': 'same', 'status': 'unknown', 'outcome': 'timeout'}
        with self.assertRaisesRegex(ValueError, 'duplicate theorem'):
            self.read_fixture([('same', cols), ('same', cols)])

    def test_repetitions_match_eligibility_before_averaging(self):
        good = plot.Result('true', 'proved', 1)
        fail = plot.Result('unknown', 'proof_timeout', 10)
        excluded = plot.Result('unknown', 'excluded', None)
        runs = [
            {'hammer': {'a': good, 'b': good, 'c': good},
             'other': {'a': fail, 'b': good, 'c': good}},
            {'hammer': {'a': fail, 'b': good, 'c': excluded},
             'other': {'a': good, 'b': good, 'c': good}},
        ]
        tasks, solved, coverage = plot.matched_runs(runs, ['hammer', 'other'])
        self.assertEqual(tasks, {'a', 'b'})
        self.assertEqual([len(s['hammer']) for s in solved], [2, 1])
        self.assertEqual(coverage[0]['removed_for_matching'], 1)
        self.assertEqual(coverage[1]['removed_for_matching'], 0)
        with tempfile.TemporaryDirectory() as temporary, patch.object(plot, 'plot_average'), patch.object(plot, 'plot_scatter'):
            summary, _, _ = plot.average_scope(runs, ['hammer', 'other'], 'hammer',
                                               Path(temporary), 'Test', 10, {})
            h = summary['provers']['hammer']
            self.assertEqual(h['solved']['mean'], 1.5)
            self.assertAlmostEqual(h['solved']['stdev'], math.sqrt(.5))
            self.assertEqual(h['solved_every_run'], 1)
            self.assertEqual(h['solved_any_run'], 2)
            self.assertEqual(summary['provers']['other']['both_with_baseline']['per_run'], [1, 1])
            self.assertEqual(summary['provers']['other']['only_prover']['per_run'], [0, 1])
            self.assertEqual(h['only_prover']['per_run'], [0, 0])
            self.assertTrue((Path(temporary) / 'problems-hammer.csv').exists())

    def test_mean_curve_counts_failures_as_unsolved_and_retains_fractional_counts(self):
        self.assertEqual(plot.average_curve([[1, 4], [2]]), [(0.5, 1), (1.0, 2), (1.5, 4)])
        self.assertEqual(plot.average_curve([[1], []]), [(0.5, 1)])
        self.assertEqual(plot.average_curve([[], []]), [])

    def test_theoretical_portfolio_uses_fastest_success_and_counts_each_theorem_once(self):
        good = lambda seconds: plot.Result('true', 'proved', seconds)
        fail = plot.Result('unknown', 'proof_timeout', .01)
        data = {'hammer': {'a': good(4), 'b': fail, 'c': fail},
                'other': {'a': good(1), 'b': good(2), 'c': fail}}
        portfolio = plot.portfolio_results(data, list(data), {'a', 'b', 'c'})
        self.assertEqual(portfolio['a'].seconds, 1)
        self.assertEqual(portfolio['b'].seconds, 2)
        self.assertFalse(portfolio['c'].solved)
        self.assertIsNone(portfolio['c'].seconds)
        with tempfile.TemporaryDirectory() as temporary, patch.object(plot, 'plot_cactus'), patch.object(plot, 'plot_scatter'):
            summary, _ = plot.make_scope(data, list(data), Path(temporary), 'Test', 'hammer', 10, {})
            self.assertEqual(summary['portfolio']['proved'], 2)
            self.assertEqual(summary['portfolio']['additional_vs_baseline'], 1)
            self.assertEqual(summary['only_this_prover']['other'], 1)

    def test_portfolio_is_unioned_within_each_run_before_averaging(self):
        good = lambda seconds: plot.Result('true', 'proved', seconds)
        fail = plot.Result('unknown', 'proof_timeout', 10)
        runs = [
            {'hammer': {'a': good(4), 'b': fail, 'c': fail},
             'other': {'a': good(1), 'b': good(2), 'c': fail}},
            {'hammer': {'a': fail, 'b': fail, 'c': good(3)},
             'other': {'a': fail, 'b': fail, 'c': good(5)}},
        ]
        with tempfile.TemporaryDirectory() as temporary, patch.object(plot, 'plot_average') as render, patch.object(plot, 'plot_scatter'):
            summary, _, rows = plot.average_scope(runs, ['hammer', 'other'], 'hammer',
                                                  Path(temporary), 'Test', 10, {})
            best = summary['provers'][plot.PORTFOLIO]
            self.assertEqual(best['solved']['per_run'], [2, 1])
            self.assertEqual(best['solved']['mean'], 1.5)
            self.assertEqual(best['solved_any_run'], 3)
            self.assertEqual(best['solved_every_run'], 0)
            self.assertIsNone(best['unique'])
            self.assertEqual(summary['provers']['other']['unique']['mean'], .5)
            self.assertEqual(render.call_args.args[0][plot.PORTFOLIO], [[1, 2], [3]])
            self.assertEqual(rows[-1][0], plot.PORTFOLIO)

    def test_summary_crosscheck_detects_stale_counts(self):
        data = {'hammer': {'a': plot.Result('true', 'proved', 1)}}
        report, _, _ = plot.summarize(data, ['hammer'])
        source = dict(report, proved={'hammer': 0})
        issues = []
        plot.check_collected(source, data, report, issues)
        self.assertEqual(issues, ['Collected JSON disagrees with XML: proved'])

    def test_scatter_includes_one_sided_and_mixed_successes_but_excludes_neither(self):
        good = lambda seconds: plot.Result('true', 'proved', seconds)
        fail = plot.Result('unknown', 'proof_timeout', 10)
        runs = [
            {'hammer': {'a': good(1), 'unstable': good(2), 'failed': fail, 'neither': fail},
             'other': {'a': good(4), 'unstable': good(3), 'failed': good(1), 'neither': fail}},
            {'hammer': {'a': good(3), 'unstable': good(2), 'failed': fail, 'neither': fail},
             'other': {'a': good(6), 'unstable': fail, 'failed': good(1), 'neither': fail}},
        ]
        points = plot.scatter_points(runs, ['hammer', 'other'], 'hammer', {'a', 'unstable', 'failed', 'neither'})
        self.assertEqual(points, {'other': [('a', 2, 5, 2, 2), ('failed', None, 1, 0, 2), ('unstable', 2, 3, 2, 1)]})
        self.assertEqual(plot.scatter_points(runs, ['hammer', 'other'], 'hammer', {'neither'}),
                         {'other': []})

    def test_compact_table_uses_all_solver_uniques_instead_of_pairwise_counts(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / 'comparison.csv').write_text(
                'prover,mean_solved,mean_unique,mean_only_prover\n'
                'vampire-hammer,10.5,4,0\nother,8.5,2,3.5\n'
                'theoretical-best,14,,3.5\n')
            self.assertEqual(plot.cactus_table_rows(folder),
                             [('vampire-hammer', 10.5, 4), ('other', 8.5, 2),
                              ('theoretical-best', 14, None)])
            (folder / 'comparison.csv').write_text(
                'Prover,Solved,Unique among selected,Only prover\n'
                'vampire-hammer,11,4,0\nother,9,2,4\ntheoretical-best,15,,4\n')
            self.assertEqual(plot.cactus_table_rows(folder),
                             [('vampire-hammer', 11, 4), ('other', 9, 2),
                              ('theoretical-best', 15, None)])

    def test_unsolved_jitter_is_reproducible_and_stays_in_its_categorical_band(self):
        value = plot.unsolved_coordinate('problem', 'duper', 'x', 100)
        self.assertEqual(value, plot.unsolved_coordinate('problem', 'duper', 'x', 100))
        self.assertNotEqual(value, plot.unsolved_coordinate('other_problem', 'duper', 'x', 100))
        self.assertNotEqual(value, plot.unsolved_coordinate('problem', 'duper', 'y', 100))
        self.assertGreaterEqual(value, 100 * 10 ** -.4)
        self.assertLessEqual(value, 100 * 10 ** .4)


if __name__ == '__main__':
    unittest.main()
