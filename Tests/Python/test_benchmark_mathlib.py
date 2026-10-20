import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


spec = importlib.util.spec_from_file_location("benchmark_mathlib", Path(__file__).resolve().parents[2] / "scripts/benchmark-mathlib.py")
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


class BenchmarkTests(unittest.TestCase):

    def test_sampling_balances_modules_and_is_reproducible(self):
        rows = [{"name": f"{module}.{index}", "module": module}
                for module in ("A", "B", "C") for index in range(20)]
        selected = benchmark.sample(rows, 8, "seed")
        self.assertEqual(selected, benchmark.sample(list(reversed(rows)), 8, "seed"))
        counts = [sum(row["module"] == module for row in selected) for module in ("A", "B", "C")]
        self.assertLessEqual(max(counts) - min(counts), 1)
        self.assertEqual(len({row["name"] for row in selected}), 8)

    def test_focused_selection_keeps_original_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            corpus = Path(temporary)
            rows = [{"id": 42, "name": "A"}, {"id": 105, "name": "B"}]
            (corpus / "lemmas.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
            names = corpus / "names.txt"
            names.write_text("B\nB\n")
            self.assertEqual(benchmark.select_from_corpus(corpus, names, 5000), [rows[1]])
            names.write_text("missing\n")
            with self.assertRaises(ValueError):
                benchmark.select_from_corpus(corpus, names, 5000)

    def test_search_budget_is_scoped_to_the_benchmark_case(self):
        source = benchmark.case_command({"id": 42, "name": "A"}, Path("run"), Path("events"), 20)
        self.assertTrue(source.startswith('set_option vampire.timeLimit 20 in\n'))
        self.assertIn('set_option vampire.outputPath "run/problems/00042.p" in\n', source)
        default = benchmark.case_command({"id": 42, "name": "A"}, Path("run"), Path("events"))
        self.assertTrue(default.startswith('set_option vampire.timeLimit 5 in\n'))

    def test_failure_categories_distinguish_search_and_replay(self):
        for message, expected in [
            ("Monomorphization failed because of a dependent type", "unsupported_input"),
            ("Auto.Reif.resolveTy :: Unable to resolve a dependent type", "unsupported_input"),
            ("Vampire timed out after 5 s", "search_timeout"),
            ("Vampire proof-search budget exhausted", "search_timeout"),
            ("Vampire aborted by signal SIGABRT on; no complete TSTP proof was produced.", "process_error"),
            ("Could not start Vampire executable `/missing`: file does not exist", "process_error"),
            ("Mock Vampire failed (2): initialization failure", "process_error"),
            ("Vampire did not produce a refutation (SZS status Satisfiable).", "search_unsuccessful"),
            ("Could not decode Vampire TPTP proof", "replay_error"),
            ("Could not parse Vampire output: malformed certificate", "replay_error"),
            ("Could not preprocess Vampire TSTP proof: unsupported higher-order step", "replay_error"),
            ("choice operator `s1` has an unsupported type or missing inhabitant", "replay_error"),
            ("difference witness requires function operands", "replay_error"),
            ("local replay expected `s1` to denote a lean-auto atom", "replay_error"),
            ("TPTP input step `f1` does not match local source fact `fact0`", "replay_error"),
        ]:
            self.assertEqual(benchmark.failure_kind({"status": "error", "error": message}), expected)

    def test_baseline_unsolved_goals_are_expected_search_failures(self):
        for prover, message in [('lean-auto-duper', 'Duper saturated'), ('duper', 'Duper saturated'), ('aesop', 'unsolved goals'),
                                ('simp_all', 'unsolved goals')]:
            self.assertEqual(benchmark.failure_kind({'status': 'error', 'prover': prover, 'error': message}),
                             'search_unsuccessful')

    def test_saved_query_variants_include_routed_and_retry_inputs_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            problem = Path(temporary) / '00000.p'
            names = ('00000.p', '00000.p.branch-2.p', '00000.p.fo.p',
                     '00000.p.ho-first', '00000.p.fo-second',
                     '00000.p.branch-2.p.before-ho.fo-first')
            for name in names:
                (problem.parent / name).write_text('tff(a, axiom, p).\n')
            # These are artifacts attached to a query, never queries themselves.
            for suffix in ('.stdout', '.events.jsonl', '.fmb.stdout'):
                Path(str(problem) + suffix).write_text('diagnostic\n')
            self.assertEqual([path.name for path in benchmark.vampire_problem_files(problem)], [
                '00000.p', '00000.p.branch-2.p', '00000.p.fo.p',
                '00000.p.branch-2.p.before-ho.fo-first', '00000.p.fo-second', '00000.p.ho-first'])

    def test_lean_literals_preserve_supplementary_unicode(self):
        source = benchmark.case_command(
            {"id": 42, "name": "ContinuousLinearMap.extendTo𝕜'_apply"},
            Path("run-𝕜"), Path("events-𝕜"),
        )
        self.assertIn("ContinuousLinearMap.extendTo𝕜'_apply", source)
        self.assertIn('"run-𝕜/problems/00042.p"', source)
        self.assertNotIn("\\ud835", source)


if __name__ == "__main__":
    unittest.main()
