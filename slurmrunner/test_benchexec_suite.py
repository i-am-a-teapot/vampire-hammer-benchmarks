import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import subprocess
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent


def load(name, filename):
    loader = importlib.machinery.SourceFileLoader(name, str(HERE / filename))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


runner = load("run_hammer_task", "run-hammer-task")
collector = load("collect_results", "collect-results.py")
suite = load("make_suite", "make-suite.py")


class RepositoryTests(unittest.TestCase):
    def test_compact_snapshot_retains_source_content_and_hashes(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / 'project'
            project.mkdir()
            source = project / 'Source.lean'
            source.write_text('theorem example : True := True.intro\n')
            output = Path(temporary) / 'suite'
            output.mkdir()
            with patch.object(suite, 'PROJECT', project), \
                    patch.object(suite.benchmark, 'source_files', return_value=[('hammer/Source.lean', source)]):
                hashes = suite.snapshot(output, compact=True)
            self.assertFalse((output / 'source-snapshot').exists())
            self.assertEqual(hashes['hammer/Source.lean'], suite.hashlib.sha256(source.read_bytes()).hexdigest())
            with suite.tarfile.open(output / 'source-snapshot.tar.gz') as archive:
                self.assertEqual(archive.extractfile('hammer/Source.lean').read(), source.read_bytes())

    def test_compact_tasks_roundtrip_without_per_theorem_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            rows = [{'id': idx, 'name': name, 'module': 'M'} for idx, name in (
                (0, 'Nat.add_comm'), (1, "List.antidiagonal'"), (7, 'Unicode.α'))]
            shards = suite.write_tasks(rows, output, 2, compact=True)
            self.assertFalse((output / 'tasks').exists())
            self.assertEqual(len(list(output.iterdir())), 1)
            args = types.SimpleNamespace(case_option=[], keep_artifacts=False, comparison=True,
                                         compact=True, shard_size=2, search_seconds=[10], timelimit='180 s',
                                         memlimit='8 GB', cpu_cores=1, hardtimelimit=None, walltimelimit='240 s')
            xml = output / 'benchmark.xml'
            xml.write_text(suite.benchmark_xml(args, output / 'suite-config.json', shards))
            parsed = slurmbench.parse_xml(xml)
            self.assertEqual(len(parsed['jobs']), len(rows) * len(suite.PROVERS))
            for prover in suite.PROVERS:
                jobs = [job for job in parsed['jobs'] if prover in job['args']]
                self.assertEqual({job['theorem'] for job in jobs}, {row['name'] for row in rows})
                for job in jobs:
                    self.assertEqual(job['args'][-2:], ['--theorem', job['theorem']])
                    self.assertIn('theorems.jsonl#', job['input'])

    def test_comparison_suite_generates_time_limited_runs_per_theorem(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / 'project'
            binary = project / '.lake/vampire/build/vampire'
            binary.parent.mkdir(parents=True)
            binary.write_text('#!/bin/sh\nexit 0\n')
            binary.chmod(0o755)
            output = Path(temporary) / 'suite'
            with patch.object(suite, 'PROJECT', project), patch.object(suite, 'LAKE', suite.LAKE), \
                    patch.object(suite.benchmark, 'PROJECT', suite.benchmark.PROJECT), \
                    patch.object(sys, 'argv', ['make-suite.py', '--comparison', '--output', str(output)]), \
                    patch.object(suite, 'check_built'), \
                    patch.object(suite, 'lean_environment', return_value=('/toolchain/bin/lean', {})), \
                    patch.object(suite, 'native_lean_args', return_value=['--plugin=/comparison.so']) as native, \
                    patch.object(suite, 'hammer_revision', return_value='revision'), \
                    patch.object(suite, 'output_of', return_value='Lean 4.34.1'), \
                    patch.object(suite.subprocess, 'check_output', return_value='mathlib-revision\n'), \
                    patch.object(suite, 'parser_dynlib', return_value=project / 'parser.so'), \
                    patch.object(suite, 'snapshot', return_value={}), \
                    patch.object(suite, 'select', return_value=([
                        {'id': 0, 'name': 'Nat.add_comm', 'module': 'Mathlib.Data.Nat.Basic'}], 1)):
                self.assertEqual(suite.main(), 0)
            cfg = json.loads((output / 'suite-config.json').read_text())
            self.assertEqual(cfg['provers'], list(suite.PROVERS))
            self.assertEqual(cfg['diagnostic_provers'], ['vampire-hammer'])
            self.assertEqual(cfg['evaluation_strategy'], 'lean-auto-style')
            self.assertIn('maxHeartbeats=0', cfg['case_options'])
            for option in suite.benchmark.COMPARISON_HEARTBEAT_OPTIONS:
                self.assertIn(f'{option}=0', cfg['case_options'])
            self.assertFalse(any(pair.startswith(('auto.mono.ciInstDefEq.maxHeartbeats=',
                                                 'auto.mono.termLikeDefEq.maxHeartbeats=',
                                                 'vampire.monoHeartbeats=')) for pair in cfg['case_options']))
            self.assertNotIn('maxHeartbeats=65536', cfg['case_options'])
            self.assertEqual(cfg['proof_timeout_seconds'], 10)
            self.assertEqual(cfg['process_wall_seconds'], 600)
            native.assert_called_once_with(imports=suite.COMPARISON_IMPORTS)
            manifest = slurmbench.parse_xml(output / 'vampire-hammer.xml')
            self.assertEqual(len(manifest['jobs']), len(suite.PROVERS))
            self.assertEqual({job['args'][job['args'].index('--prover') + 1] for job in manifest['jobs']},
                             set(suite.PROVERS))
            self.assertEqual(len({job['input'] for job in manifest['jobs']}), 1)

    def test_six_prover_xml_selects_isolated_config_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            args = types.SimpleNamespace(case_option=[], keep_artifacts=False, comparison=True,
                                         lean_auto_project=output / 'upstream', compact=True,
                                         shard_size=1000, search_seconds=[10], timelimit='300 s',
                                         memlimit='8 GB', cpu_cores=1, hardtimelimit=None, walltimelimit='360 s')
            suite.write_tasks([{'id': 0, 'name': 'Nat.add_comm', 'module': 'Mathlib.Data.Nat.Basic'}], output, 1000, compact=True)
            xml = output / 'benchmark.xml'
            xml.write_text(suite.benchmark_xml(args, output / 'suite-config.json', [0]))
            manifest = slurmbench.parse_xml(xml)
            self.assertEqual(len(manifest['jobs']), 6)
            for job in manifest['jobs']:
                self.assertEqual(job['args'].count('--config'), 1)
                prover = job['args'][job['args'].index('--prover') + 1]
                config = job['args'][job['args'].index('--config') + 1]
                self.assertEqual(Path(config).name, 'lean-auto-config.json' if prover == 'lean-auto-duper' else 'suite-config.json')

    def test_seven_prover_xml_selects_smt_isolated_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            args = types.SimpleNamespace(case_option=[], keep_artifacts=False, comparison=True,
                                         lean_auto_project=output / 'upstream', lean_smt_project=output / 'smt',
                                         compact=True, shard_size=1000, search_seconds=[10], timelimit='300 s',
                                         memlimit='8 GB', cpu_cores=1, hardtimelimit=None, walltimelimit='360 s')
            suite.write_tasks([{'id': 0, 'name': 'Nat.add_comm', 'module': 'Mathlib.Data.Nat.Basic'}], output, 1000, compact=True)
            xml = output / 'benchmark.xml'
            xml.write_text(suite.benchmark_xml(args, output / 'suite-config.json', [0]))
            jobs = slurmbench.parse_xml(xml)['jobs']
            self.assertEqual(len(jobs), 7)
            for job in jobs:
                prover = slurmbench.job_prover(job)
                self.assertEqual(job['args'].count('--config'), 1)
                config = job['args'][job['args'].index('--config') + 1]
                expected = {'lean-smt': 'lean-smt-config.json', 'lean-auto-duper': 'lean-auto-config.json'}.get(prover, 'suite-config.json')
                self.assertEqual(Path(config).name, expected)

    def test_prepare_smt_preserves_kernel_audit_and_pins_solver(self):
        preparation = load('prepare_lean_smt', 'prepare-lean-smt.py')
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'project'
            with patch.object(sys, 'argv', ['prepare-lean-smt.py', '--output', str(output)]):
                preparation.main()
            harness = (output / 'VampireHammerBenchmark/Mathlib.lean').read_text()
            tactic = (output / 'VampireHammerBenchmark/Comparison.lean').read_text()
            for fragment in ('auditDependencies proof target', 'proofPremisesOf info', 'addDecl <| .thmDecl'):
                self.assertIn(fragment, harness)
            self.assertNotIn('VampireHammer.Replay', harness)
            self.assertIn('(env.setExporting false).find? target', harness)
            self.assertIn('baseline_import_dependency', harness)
            self.assertIn('if targets.isEmpty then return #[]', harness)
            self.assertIn('smt +mono -trust -native [*, $[$hints],*]', tactic)
            self.assertIn(preparation.SMT_REV, (output / 'lakefile.lean').read_text())
            self.assertIn(preparation.CVC5_REV, (output / 'lakefile.lean').read_text())
            work = output / 'case'
            work.mkdir()
            source, _ = runner.write_case(work, 'Nat.add_comm', '/unused', '/unused', 10, '',
                                         'lean-auto-style', 'lean-smt', suite.COMPARISON_IMPORTS, True)
            text = source.read_text()
            for option in ('executable', 'tptpParserDynlib', 'timeLimit', 'diagnosticCase', 'outputPath'):
                self.assertNotIn('set_option vampire.' + option, text)
            self.assertIn('set_option synthInstance.maxHeartbeats 0', text)

    def test_smt_comparison_keeps_missing_and_excluded_cases_out(self):
        with tempfile.TemporaryDirectory() as temporary:
            completed = []
            for prover in (*suite.PROVERS, 'lean-auto-duper', 'lean-smt'):
                for task in ('A', 'B'):
                    job = {'input': task, 'args': ['--prover', prover, '--search-seconds', '10']}
                    excluded = prover == 'lean-smt' and task == 'B'
                    completed.append((job, {'status': 'unknown' if excluded else 'true',
                                            'extracted': {'outcome': 'excluded' if excluded else 'proved'}}, None))
            slurmbench.comparison_diagnostics(Path(temporary), completed)
            report = json.loads((Path(temporary) / 'comparison-search10.json').read_text())
            self.assertEqual(report['common_completed'], 2)
            self.assertEqual(report['common_eligible'], 1)
            self.assertEqual(report['proved']['lean-smt'], 1)
            self.assertEqual(len(report['provers']), 7)
            slurmbench.comparison_diagnostics(Path(temporary), completed[:-2], report['provers'])
            report = json.loads((Path(temporary) / 'comparison-search10.json').read_text())
            self.assertEqual(report['common_completed'], 0)
            self.assertIn('lean-smt', report['provers'])

    def test_upstream_config_rejects_hammer_fork(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            harness = suite.PROJECT / 'VampireHammerBenchmark/Mathlib.lean'
            (project / 'upstream-benchmark.json').write_text(json.dumps({
                'harness_sha256': suite.hashlib.sha256(harness.read_bytes()).hexdigest()}))
            (project / 'lake-manifest.json').write_text(json.dumps({'packages': [{
                'name': 'auto', 'url': 'https://github.com/i-am-a-teapot/lean-auto',
                'rev': 'a2f825aca7e56ac2f6b3b502bc1290369fc6d5f5'}]}))
            original = suite.PROJECT
            with self.assertRaisesRegex(SystemExit, 'normal upstream'):
                suite.upstream_config(project, {})
            self.assertEqual(suite.PROJECT, original)

    def test_source_snapshot_provenance_works_without_git(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / '.benchmark-source.json').write_text(json.dumps({'revision': 'abcde123456789', 'dirty': [' M source']}))
            with patch.object(suite, 'PROJECT', project), patch.object(suite, 'output_of') as git:
                self.assertEqual(suite.hammer_revision(), 'abcde1234567-dirty-snapshot')
            git.assert_not_called()

    def test_cpu_control_reading_and_strict_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'cpufreq').mkdir()
            (root / 'smt').mkdir()
            (root / 'cpufreq/boost').write_text('0\n')
            for state in ('off', 'forceoff', 'notsupported'):
                (root / 'smt/control').write_text(state + '\n')
                values = slurmbench.read_cpu_controls(root)
                self.assertEqual(values['boost']['value'], '0')
                self.assertEqual(slurmbench.cpu_control_errors(values), [])
            (root / 'smt/control').write_text('on\n')
            self.assertTrue(slurmbench.cpu_control_errors(slurmbench.read_cpu_controls(root)))
            (root / 'smt/control').write_text('off\n')
            (root / 'cpufreq/boost').write_text('1\n')
            self.assertTrue(slurmbench.cpu_control_errors(slurmbench.read_cpu_controls(root)))
            missing = slurmbench.read_cpu_controls(root / 'missing')
            self.assertIn('read_error', missing['boost'])
            self.assertEqual(len(slurmbench.cpu_control_errors(missing)), 2)

    def test_worker_records_cpu_control_failure_before_running(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / 'manifest.json'
            manifest.write_text(json.dumps({'output': str(root), 'packed_nodes': 1,
                                           'packed_cpus_per_node': 1, 'shards': 1,
                                           'require_no_boost_no_smt': True, 'jobs': [],
                                           'scratch_root': '/scratch'}))
            unsafe = {'boost': {'path': '/sys/devices/system/cpu/cpufreq/boost', 'value': '1'},
                      'smt': {'path': '/sys/devices/system/cpu/smt/control', 'value': 'on'}}
            ns = types.SimpleNamespace(manifest=str(manifest), shard=0)
            with patch.dict(os.environ, {'SLURM_PROCID': '0'}), \
                    patch.object(slurmbench.os, 'sched_getaffinity', return_value={0}), \
                    patch.object(slurmbench.shutil, 'which', return_value='/usr/bin/taskset'), \
                    patch.object(slurmbench, 'read_cpu_controls', return_value=unsafe), \
                    patch.object(slurmbench, 'stage_hammer') as staging, \
                    patch.object(slurmbench, 'run_batch') as execute:
                with self.assertRaisesRegex(ValueError, 'CPU-control preflight failed'):
                    slurmbench.packed_worker(ns)
            staging.assert_not_called()
            execute.assert_not_called()
            saved = json.loads(next((root / 'node-environments').glob('*.json')).read_text())
            self.assertEqual(saved['cpu_controls'], unsafe)
            self.assertTrue(saved['cpu_control_check']['required'])
            self.assertEqual(len(saved['cpu_control_check']['errors']), 2)

    def test_physical_core_selection_skips_smt_siblings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for cpu, core in ((0, 0), (1, 0), (2, 1), (3, 1)):
                topology = root / f'cpu{cpu}/topology'
                topology.mkdir(parents=True)
                (topology / 'physical_package_id').write_text('0')
                (topology / 'core_id').write_text(str(core))
            self.assertEqual(slurmbench.physical_core_selection([0, 1, 2, 3], 2, root), [0, 2])
            self.assertEqual(slurmbench.physical_core_selection([1, 3], 2, root), [1, 3])
            with self.assertRaisesRegex(ValueError, 'Only 2 physical cores'):
                slurmbench.physical_core_selection([0, 1, 2, 3], 3, root)
            with self.assertRaisesRegex(ValueError, 'Cannot verify'):
                slurmbench.physical_core_selection([4], 1, root)

    def test_batches_never_mix_tactics_modules_configs_or_budgets(self):
        def job(name, prover='duper', module='M', config='main.json', seconds='10'):
            return {'theorem': name, 'args': ['--config', config, '--prover', prover,
                                            '--search-seconds', seconds, '--module', module,
                                            '--theorem', name]}
        jobs = [job('A'), job('B'), job('C'), job('D', prover='lean-auto-duper'),
                job('E', prover='lean-auto-duper', config='upstream.json'),
                job('F', module='Other'), job('G', seconds='20')]
        groups = slurmbench.batches({'jobs': jobs}, 2)
        self.assertEqual(groups, [[0, 1], [2], [3], [4], [5], [6]])
        self.assertEqual([idx for group in groups for idx in group], list(range(len(jobs))))

    def test_upstream_case_omits_unregistered_vampire_options(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, _ = runner.write_case(Path(temporary), 'Nat.add_comm', '/unused', '/unused',
                                         10, 'set_option maxHeartbeats 0 in\n', 'lean-auto-style',
                                         'lean-auto-duper', suite.COMPARISON_IMPORTS, True,
                                         'Mathlib.Data.Nat.Basic')
            text = source.read_text()
            self.assertIn('hammer_mathlib_try_with_premises_in_module', text)
            self.assertIn('set_option vampire.benchmarkProver "lean-auto-duper"', text)
            self.assertIn('set_option synthInstance.maxHeartbeats 0\n', text)
            for option in ('executable', 'tptpParserDynlib', 'timeLimit', 'diagnosticCase', 'outputPath', 'profileLive'):
                self.assertNotIn('set_option vampire.' + option, text)

    def test_comparison_preserves_vampire_internal_heartbeat_windows(self):
        self.assertNotIn('vampire.monoHeartbeats', runner.benchmark.COMPARISON_HEARTBEAT_OPTIONS)
        for prover in (*suite.PROVERS, 'lean-auto-duper', 'lean-smt'):
            with self.subTest(prover=prover), tempfile.TemporaryDirectory() as temporary:
                source, _ = runner.write_case(Path(temporary), 'Nat.add_comm', '/unused', '/unused',
                                             10, 'set_option vampire.monoHeartbeats 8000 in\n'
                                             if prover == 'vampire-hammer' else '', 'lean-auto-style',
                                             prover, suite.COMPARISON_IMPORTS, True)
                text = source.read_text()
                self.assertIn('set_option maxHeartbeats 0\n', text)
                self.assertIn('set_option synthInstance.maxHeartbeats 0\n', text)
                self.assertNotIn('set_option auto.mono.ciInstDefEq.maxHeartbeats 0', text)
                self.assertNotIn('set_option auto.mono.termLikeDefEq.maxHeartbeats 0', text)
                if prover == 'vampire-hammer':
                    self.assertIn('set_option vampire.monoHeartbeats 8000 in\n', text)
                    self.assertNotIn('set_option vampire.monoHeartbeats 0', text)

    def test_upstream_harness_preserves_replay_and_audit(self):
        preparation = load('prepare_lean_auto', 'prepare-lean-auto.py')
        text = preparation.harness_source((HERE.parent / 'VampireHammerBenchmark/Mathlib.lean').read_text())
        self.assertNotIn('import VampireHammer\n', text)
        self.assertNotIn('by vampire', text)
        self.assertNotIn('VampireHammer.Replay', text)
        for fragment in ('addDecl <| .thmDecl', 'auditDependencies proof target',
                         'proofPremisesOf info', 'replayDeclarations targets', 'same_command_dependency'):
            self.assertIn(fragment, text)

    def test_missing_upstream_results_keep_six_way_denominator(self):
        with tempfile.TemporaryDirectory() as temporary:
            completed = []
            for prover in suite.PROVERS:
                job = {'input': 'A', 'args': ['--prover', prover, '--search-seconds', '10'], 'theorem': 'A'}
                completed.append((job, {'status': 'true', 'extracted': {'outcome': 'proved'}}, None))
            slurmbench.comparison_diagnostics(Path(temporary), completed, [*suite.PROVERS, 'lean-auto-duper'])
            report = json.loads((Path(temporary) / 'comparison-search10.json').read_text())
            self.assertIn('lean-auto-duper', report['provers'])
            self.assertEqual(report['common_completed'], 0)
            self.assertEqual(report['available_comparison']['proved']['duper'], 1)

    def test_lake_environment_uses_benchmark_package(self):
        commands = []

        def output_of(command, **kwargs):
            commands.append(command)
            if command == ["env", "-0"]:
                return "PATH=/bin\0"
            if command[-1] == "--print-prefix":
                return "/toolchain"
            return "PATH=/lake/bin\0LEAN_PATH=/repo/.lake/build/lib/lean\0"

        with patch.object(suite, "output_of", side_effect=output_of):
            lean, env = suite.lean_environment()
        self.assertEqual(lean, "/toolchain/bin/lean")
        self.assertEqual(env, {"LEAN_PATH": "/repo/.lake/build/lib/lean"})
        for command in commands[1:]:
            self.assertEqual(command[:2], ["lake", "-R"])

    def test_suite_targets_separate_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "vampire-hammer"
            binary = project / ".lake/vampire/build/vampire"
            binary.parent.mkdir(parents=True)
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(0o755)
            harness = project / "VampireHammerBenchmark/Mathlib.lean"
            harness.parent.mkdir(parents=True)
            harness.write_text("-- benchmark harness\n")
            (project / "lean-toolchain").write_text("leanprover/lean4:v4.34.1\n")
            output = Path(temporary) / "suite"
            rows = [{"id": 0, "name": "Nat.add_comm", "module": "Mathlib.Data.Nat.Basic"}]
            with patch.object(suite, "PROJECT", suite.PROJECT), \
                    patch.object(suite.benchmark, "PROJECT", suite.benchmark.PROJECT), \
                    patch.object(sys, "argv", ["make-suite.py", "--project", str(project), "--output", str(output),
                                               "--strategy", "lean-auto-style"]), \
                    patch.object(suite, "check_built"), \
                    patch.object(suite, "lean_environment", return_value=("/toolchain/bin/lean", {})), \
                    patch.object(suite, "native_lean_args", return_value=["--plugin=/lib/hammer.so"]), \
                    patch.object(suite, "hammer_revision", return_value="new-revision"), \
                    patch.object(suite, "output_of", return_value="Lean 4.34.1"), \
                    patch.object(suite.subprocess, "check_output", return_value="mathlib-revision\n"), \
                    patch.object(suite, "parser_dynlib", return_value=project / "parser.so"), \
                    patch.object(suite, "select", return_value=(rows, 1)):
                self.assertEqual(suite.main(), 0)
                self.assertEqual(suite.benchmark.PROJECT, project)
            config = json.loads((output / "suite-config.json").read_text())
            self.assertEqual(config["project"], str(project))
            self.assertEqual(config["vampire"], str(binary))
            self.assertEqual(config["hammer_revision"], "new-revision")
            self.assertEqual(config["lean_args"], ["--plugin=/lib/hammer.so"])
            self.assertEqual(config["evaluation_strategy"], "lean-auto-style")
            self.assertEqual(config["success_seconds"], 10)
            self.assertEqual(config["search_seconds"], 10)
            self.assertIn("maxHeartbeats=0", config["case_options"])
            self.assertIn("vampire.omitUnsupportedLocalHypotheses=true", config["case_options"])
            self.assertIn("vampire.alignNestedDefinitions=true", config["case_options"])
            self.assertIn("project/VampireHammerBenchmark/Mathlib.lean", config["source_hashes"])
            self.assertIn("project/lean-toolchain", config["source_hashes"])
            self.assertIn(str(suite.TOOL_PROJECT / "slurmrunner/run-hammer-task"),
                          (output / "vampire-hammer.xml").read_text())

    def test_runner_fallback_uses_benchmark_package(self):
        command, env = runner.lean_command({}, Path("case.lean"))
        self.assertIn("lake", command)
        self.assertIn("lean", command)
        # `lake lean` loads the native libraries; `lake env lean` would not.
        self.assertNotIn("env", command)
        self.assertEqual(command[command.index("lean") + 1], "case.lean")
        self.assertIsNone(env)

    def test_runner_passes_recorded_native_flags(self):
        config = {"lean": "/toolchain/bin/lean", "lean_env": {},
                  "lean_args": ["--load-dynlib=/lib/parser.so", "--plugin=/lib/hammer.so"]}
        command, _ = runner.lean_command(config, Path("case.lean"))
        start = command.index("/toolchain/bin/lean")
        self.assertEqual(command[start + 1:start + 3], config["lean_args"])
        self.assertEqual(command[-1], "case.lean")

    def test_native_flags_follow_lakes_module_setup(self):
        setup = {"plugins": [{"path": "/lib/hammer.so"}], "dynlibs": ["/lib/parser.so"]}
        result = subprocess.CompletedProcess([], 0, "", "trace: module setup: " + json.dumps(setup) + "\n")
        with patch.object(suite.subprocess, "run", return_value=result) as run:
            flags = suite.native_lean_args()
        self.assertIn("lean", run.call_args.args[0])
        # Plugins can link against the dynlibs, which therefore load first.
        self.assertEqual(flags, ["--load-dynlib=/lib/parser.so", "--plugin=/lib/hammer.so"])

    def test_native_flags_reject_hammer_loaded_before_lean_auto(self):
        setup = {"plugins": [{"path": "/lib/libauto_Auto.so"}],
                 "dynlibs": ["/lib/libvampire__hammer_VampireHammer.so"]}
        result = subprocess.CompletedProcess([], 0, "", "trace: module setup: " + json.dumps(setup) + "\n")
        with patch.object(suite.subprocess, "run", return_value=result), \
                self.assertRaises(SystemExit) as raised:
            suite.native_lean_args()
        self.assertIn("single root", str(raised.exception))

    def test_native_probe_rejects_failed_imports_even_with_setup_trace(self):
        result = subprocess.CompletedProcess([], 1, '', 'module setup: {"plugins": []}\nerror: bad import')
        with patch.object(suite.subprocess, 'run', return_value=result), self.assertRaises(SystemExit):
            suite.native_lean_args()

    def test_duper_strategy_rejects_unfiltered_catalog(self):
        with tempfile.TemporaryDirectory() as temporary:
            catalog = Path(temporary) / "catalog.jsonl"
            catalog.write_text(json.dumps({"name": "Nat.add_comm", "module": "Mathlib.Data.Nat.Basic"}) + "\n")
            args = types.SimpleNamespace(catalog=catalog, strategy="lean-auto-style")
            with self.assertRaisesRegex(SystemExit, "requires a catalog generated with that strategy"):
                suite.catalog_rows(args, Path(temporary))

# Minimal stand-ins so the tool-info module imports without BenchExec installed.
benchexec = types.ModuleType("benchexec")
result = types.ModuleType("benchexec.result")
result.RESULT_TRUE_PROP, result.RESULT_UNKNOWN, result.RESULT_ERROR = "true", "unknown", "ERROR"
tools = types.ModuleType("benchexec.tools")
template = types.ModuleType("benchexec.tools.template")
template.BaseTool2 = object
benchexec.result, benchexec.tools, tools.template = result, tools, template
sys.modules.update({"benchexec": benchexec, "benchexec.result": result,
                    "benchexec.tools": tools, "benchexec.tools.template": template})
sys.path.insert(0, str(HERE))
from hammer_bench import tool_info  # noqa: E402


def output(row):
    return ["[run-hammer-task] theorem X; search 20s", "lean output", "HAMMER-RESULT: " + json.dumps(row)]


class RunnerTests(unittest.TestCase):
    def test_saved_native_strategy_is_a_separate_query(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            problem = work / 'problems/00000.p'
            problem.parent.mkdir()
            for suffix in ('', '.ho-first'):
                Path(str(problem) + suffix).write_text('thf(a, axiom, p).\n')
            Path(str(problem) + '.ho-first.stdout').write_text('% SZS status GaveUp\n')
            details = runner.vampire_details(work, include_output=True)
            self.assertEqual(details['vampire_queries'], 2)
            self.assertEqual(details['query_details'][1]['szs_status'], 'GaveUp')

    def test_legacy_result_with_exported_input_has_unknown_attribution(self):
        stdout = io.StringIO()
        finished = {'event': 'finished', 'name': 'T', 'status': 'proved', 'seconds': 1.2,
                    'kernel_checked': True}
        with patch.object(sys, 'argv', ['run-hammer-task', '--theorem', 'T']), \
                patch.object(sys, 'stdout', stdout), patch.object(runner.subprocess, 'Popen') as popen, \
                patch.object(runner, 'read_events', return_value=[finished]), \
                patch.object(runner, 'vampire_details', return_value={'vampire_queries': 1}):
            popen.return_value.wait.return_value = 0
            runner.main()
        row = json.loads(stdout.getvalue().split(runner.RESULT_PREFIX)[1])
        self.assertEqual(row['route'], 'unknown')
        self.assertEqual(row['route_source'], 'unavailable')

    def test_exported_failed_queries_do_not_override_accepted_proof_route(self):
        for route in ('direct', 'vampire'):
            with self.subTest(route=route):
                stdout = io.StringIO()
                finished = {'event': 'finished', 'name': 'T', 'status': 'proved', 'route': route,
                            'route_source': 'accepted_proof', 'reconstruction_modes': [] if route == 'direct' else ['fo'],
                            'seconds': 1.2, 'kernel_checked': True}
                with patch.object(sys, 'argv', ['run-hammer-task', '--theorem', 'T']), \
                        patch.object(sys, 'stdout', stdout), patch.object(runner.subprocess, 'Popen') as popen, \
                        patch.object(runner, 'read_events', return_value=[finished]), \
                        patch.object(runner, 'vampire_details', return_value={'vampire_queries': 2}):
                    popen.return_value.wait.return_value = 0
                    runner.main()
                row = json.loads(stdout.getvalue().split(runner.RESULT_PREFIX)[1])
                self.assertEqual(row['route'], route)
                self.assertEqual(row['attempted_route'], 'vampire')
                self.assertEqual(row['route_source'], 'accepted_proof')

    def test_staged_fo_only_run_is_a_vampire_query(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            problem = work / 'problems/00000.p.fo.p'
            problem.parent.mkdir()
            problem.write_text('tff(a, axiom, p).\n')
            Path(str(problem) + '.fmb.stdout').write_text('% SZS status Unknown\n')
            details = runner.vampire_details(work, include_output=True)
            self.assertEqual(details['vampire_queries'], 1)
            query = details['query_details'][0]
            self.assertEqual(query['problem'], problem.name)
            self.assertIn('Unknown', query['sat_precheck_stdout'])

    def test_baseline_runner_suppresses_diagnostics_and_artifacts(self):
        for prover in ('duper', 'aesop', 'simp_all'):
            with self.subTest(prover=prover), tempfile.TemporaryDirectory() as temporary:
                artifacts = Path(temporary) / 'artifacts'
                stdout = io.StringIO()
                finished = {'event': 'finished', 'name': 'T', 'status': 'error', 'error': 'unsolved goals',
                            'seconds': 1.2, 'kernel_checked': False, 'premises': ['P'], 'axioms': ['A']}
                with patch.object(sys, 'argv', ['run-hammer-task', '--theorem', 'T', '--prover', prover,
                                               '--keep-artifacts', '--artifacts-dir', str(artifacts)]), \
                        patch.object(sys, 'stdout', stdout), \
                        patch.object(runner.subprocess, 'Popen') as popen, \
                        patch.object(runner, 'read_events', return_value=[finished]), \
                        patch.object(runner, 'vampire_details') as details:
                    popen.return_value.wait.return_value = 0
                    self.assertEqual(runner.main(), 0)
                self.assertEqual(popen.call_args.kwargs['stdout'], subprocess.DEVNULL)
                details.assert_not_called()
                self.assertFalse(artifacts.exists())
                row = json.loads(stdout.getvalue().split(runner.RESULT_PREFIX)[1])
                self.assertEqual((row['prover'], row['failure_kind'], row['seconds']),
                                 (prover, 'search_unsuccessful', 1.2))
                self.assertTrue({'error', 'premises', 'axioms', 'query_details', 'artifacts_dir'}.isdisjoint(row))

    def test_query_diagnostics_include_branches_and_bounded_failure_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            (work / 'problems').mkdir()
            for filename, status, code in [('00000.p', 'Theorem', '0'),
                                           ('00000.p.branch-1.p', 'Unknown', '-6')]:
                problem = work / 'problems' / filename
                problem.write_text('thf(a, axiom, p).\n')
                Path(str(problem) + '.stdout').write_text(f'% SZS status {status}\n')
                Path(str(problem) + '.exitcode').write_text(code)
            stderr = work / 'problems/00000.p.branch-1.p.stderr'
            stderr.write_text('x' * 5000 + '\nInitialization failure')
            details = runner.vampire_details(work, include_output=True)
            self.assertEqual(details['vampire_queries'], 2)
            self.assertEqual((details['vampire_exit'], details['szs_status']), (0, 'Theorem'))
            branch = details['query_details'][1]
            self.assertEqual(branch['vampire_exit'], -6)
            self.assertTrue(branch['stderr_tail'].startswith('[truncated]\n'))
            self.assertTrue(branch['stderr_tail'].endswith('Initialization failure'))
            self.assertNotIn('stderr_tail', runner.vampire_details(work)['query_details'][1])
            Path(str(work / 'problems/00000.p') + '.exitcode').write_text('')
            self.assertIn('exitcode_error', runner.vampire_details(work)['query_details'][0])

    def test_wall_watchdog_times_proof_not_imports_or_audit(self):
        with tempfile.TemporaryDirectory() as temporary:
            events = Path(temporary) / 'events.jsonl'
            script = (
                'import json, pathlib, time\n'
                f'p = pathlib.Path({str(events)!r})\n'
                'time.sleep(0.2)\n'
                'p.write_text(json.dumps({"event": "started"}) + "\\n")\n'
                'time.sleep(0.01)\n'
                'with p.open("a") as f: f.write(json.dumps({"event": "proof_checked"}) + "\\n")\n'
                'time.sleep(0.2)\n')
            proc = subprocess.Popen([sys.executable, '-c', script])
            self.assertEqual(runner.wait_for_lean(proc, events, 2, 0.15), (0, None))
            events.unlink()
            started_event = json.dumps({'event': 'started'}) + '\n'
            proc = subprocess.Popen([sys.executable, '-c',
                f'import pathlib,time; pathlib.Path({str(events)!r}).write_text({started_event!r}); time.sleep(60)'])
            code, reason = runner.wait_for_lean(proc, events, 2, 0.15)
            self.assertEqual((code, reason), (-9, 'proof_timeout'))
            events.unlink()
            proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
            self.assertEqual(runner.wait_for_lean(proc, events, 0.15, 0.15), (-9, 'timeout'))

    def test_event_reader_retains_partial_records_without_rereading(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'events.jsonl'
            reader = runner.EventReader(path)
            self.assertEqual(reader.read(), [])
            path.write_bytes(b'{"event":"started",')
            self.assertEqual(reader.read(), [])
            with path.open('ab') as stream:
                stream.write(b'"name":"A"}\n{"event":"proof_checked"}\n')
            self.assertEqual(reader.read(), [{'event': 'started', 'name': 'A'}, {'event': 'proof_checked'}])
            self.assertEqual(reader.read(), [])

    @unittest.skipUnless(sys.platform.startswith('linux'), 'shared Linux monotonic clock')
    def test_proof_watchdog_accounts_for_delayed_event_observation(self):
        with tempfile.TemporaryDirectory() as temporary:
            import time
            events = Path(temporary) / 'events.jsonl'
            for batch in (False, True):
                events.write_text(json.dumps({'event': 'started', 'name': 'A',
                                             'monotonic_ns': time.monotonic_ns() - 1_000_000_000}) + '\n')
                proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
                start = time.monotonic()
                try:
                    result = runner.wait_for_batch(proc, events, 5, 0.5) if batch else runner.wait_for_lean(proc, events, 5, 0.5)
                    self.assertEqual(result[:2], (-9, 'proof_timeout'))
                    if batch:
                        self.assertEqual(result[2], 'A')
                    self.assertLess(time.monotonic() - start, 0.5)
                finally:
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait()

    def test_timeout_stops_child_and_grandchild(self):
        with tempfile.TemporaryDirectory() as temporary:
            import time
            work = Path(temporary)
            grand_pid = work / 'grandchild.pid'
            child_pid = work / 'child.pid'
            child = ('import subprocess,sys,pathlib,time; '
                     'p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"]); '
                     f'pathlib.Path({str(grand_pid)!r}).write_text(str(p.pid)); time.sleep(60)')
            parent = ('import subprocess,sys,pathlib,time; '
                      f'p=subprocess.Popen([sys.executable,"-c",{child!r}]); '
                      f'pathlib.Path({str(child_pid)!r}).write_text(str(p.pid)); time.sleep(60)')
            proc = subprocess.Popen([sys.executable, '-c', parent])
            descendants = []
            try:
                deadline = time.monotonic() + 5
                while not grand_pid.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(grand_pid.exists())
                descendants = [int(path.read_text()) for path in (child_pid, grand_pid)]
                runner.kill_tree(proc)
                self.assertEqual(proc.returncode, -9)
                for pid in descendants:
                    path = Path(f'/proc/{pid}/stat')
                    deadline = time.monotonic() + 1
                    def state():
                        try:
                            return path.read_text().rsplit(')', 1)[1].split()[0]
                        except OSError:
                            return None  # Exited between checking /proc and reading it.
                    while state() not in (None, 'Z', 'X') and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertIn(state(), (None, 'Z', 'X'))
            finally:
                for pid in [proc.pid, *descendants]:
                    try:
                        os.kill(pid, 9)
                    except ProcessLookupError:
                        pass
                proc.wait()

    def test_success_limit_rejects_invalid_measurements(self):
        for value in (float('nan'), float('inf'), -1, True):
            event = runner.apply_success_limit({'status': 'proved', 'seconds': value}, 10)
            self.assertEqual(event['status'], 'infrastructure_error')

    def test_portfolio_timeout_uses_overall_time_not_last_worker_slice(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            problem = work / 'problems/00000.p'
            problem.parent.mkdir()
            problem.write_text('tff(a, axiom, p).\n')
            Path(str(problem) + '.stdout').write_text(
                '% Termination reason: Time limit\n% Time elapsed: 0.100 s\n'
                '% Proof not found in time 9.923 s\n% SZS status Timeout for unknown\n')
            details = runner.vampire_details(work)
            self.assertEqual(details['search_seconds'], 9.923)
            self.assertEqual(details['szs_status'], 'Timeout')
            # A successful final worker retains its own elapsed time.
            Path(str(problem) + '.stdout').write_text(
                '% Proof not found in time 1.000 s\n% Time elapsed: 2.500 s\n'
                '% SZS status Theorem for unknown\n')
            self.assertEqual(runner.vampire_details(work)['search_seconds'], 2.5)

    def test_proof_budget_counts_whole_proof_time(self):
        event = {"status": "proved", "seconds": 10.1, "kernel_checked": True}
        late = runner.apply_success_limit(event, 10)
        self.assertEqual(late["status"], "proof_timeout")
        self.assertTrue(late["kernel_checked"])
        self.assertTrue(late["proved_outside_budget"])
        self.assertEqual(event["status"], "proved")
        self.assertEqual(runner.apply_success_limit({**event, "seconds": 10}, 10)["status"], "proved")
        self.assertEqual(runner.apply_success_limit(event, None)["status"], "proved")
        self.assertEqual(runner.apply_success_limit({**event, "status": "reuses_target"}, 10)["status"],
                         "reuses_target")

    def test_batch_results_stream_before_later_attempts(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            order = []

            def batch_case(directory, remaining, wrapper, parser_dynlib, search_seconds, case_options,
                           prover, imports, comparison, module):
                order.append('attempt:' + '+'.join(remaining))
                events = directory / 'events.jsonl'
                rows = [{'event': 'started', 'name': remaining[0]},
                        {'event': 'finished', 'name': remaining[0],
                         'status': 'proved', 'seconds': 0.1, 'kernel_checked': True}]
                script = ['import json, pathlib, sys',
                          f'rows = {rows!r}',
                          f'events = pathlib.Path({str(events)!r})',
                          'with events.open("a") as stream:',
                          '    for row in rows: stream.write(json.dumps(row) + "\\n")',
                          # The first attempt crashes after one target; the retry finishes the rest.
                          'sys.exit(1)' if len(remaining) > 1 else 'sys.exit(0)']
                (directory / 'case.py').write_text('\n'.join(script) + '\n')
                return directory / 'case.lean', events

            def lean_command(config, source):
                return [sys.executable, str(source.with_suffix('.py'))], None

            args = types.SimpleNamespace(wall_timeout=None, proof_timeout=None, prover='vampire-hammer')

            def report(name, entry):
                order.append('report:' + name)

            with patch.object(runner, 'write_batch_case', batch_case), \
                    patch.object(runner, 'lean_command', lean_command):
                results = runner.run_batch(args, {}, work, ['Thm.A', 'Thm.B'], 'M',
                                            lambda directory: 'wrapper', None, 10, '', work, report=report)
            # The finished target is reported before its sibling is retried, so a
            # batch killed at an outer deadline keeps every finished result.
            self.assertEqual(order, ['attempt:Thm.A+Thm.B', 'report:Thm.A',
                                     'attempt:Thm.B', 'report:Thm.B'])
            self.assertEqual(sorted(results), ['Thm.A', 'Thm.B'])
            self.assertTrue(all(entry[0]['status'] == 'proved' for entry in results.values()))

    def test_duper_strategy_generates_proof_premise_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, _ = runner.write_case(Path(temporary), "Nat.add_comm", "wrapper", "parser.so", 10,
                                          "set_option maxHeartbeats 65536 in\n", "lean-auto-style")
            self.assertIn('hammer_mathlib_try_with_premises "Nat.add_comm"', source.read_text())
            self.assertIn("set_option maxHeartbeats 65536 in", source.read_text())

    def test_terminal_event_wins(self):
        events = [{"event": "started", "name": "X"}, {"event": "finished", "name": "X", "status": "proved"}]
        self.assertEqual(runner.classify("X", events, 0)["status"], "proved")

    def test_started_without_finish_is_process_error(self):
        self.assertEqual(runner.classify("X", [{"event": "started", "name": "X"}], 1)["status"], "process_error")

    def test_no_events_is_infrastructure_error(self):
        self.assertEqual(runner.classify("X", [], 1)["status"], "infrastructure_error")


class ToolInfoTests(unittest.TestCase):
    def status(self, row):
        run = types.SimpleNamespace(output=output(row))
        return tool_info.Tool().determine_result(run)

    def test_statuses(self):
        self.assertEqual(self.status({"status": "proved"}), "true")
        self.assertEqual(self.status({"status": "error", "failure_kind": "search_timeout"}), "unknown")
        self.assertEqual(self.status({"status": "proof_timeout", "failure_kind": "proof_timeout"}), "unknown")
        self.assertEqual(self.status({"status": "reuses_target", "failure_kind": "reuses_target"}),
                         "ERROR (reuses_target)")
        self.assertEqual(tool_info.Tool().determine_result(types.SimpleNamespace(output=["killed"])), "ERROR")

    def test_columns(self):
        row = {"status": "proved", "failure_kind": "proved", "route": "direct", "seconds": 1.5}
        tool = tool_info.Tool()
        self.assertEqual(tool.get_value_from_output(output(row), "route"), "direct")
        self.assertEqual(tool.get_value_from_output(output(row), "hammer-seconds"), 1.5)

    def test_ignores_malformed_or_non_object_result_lines(self):
        run = types.SimpleNamespace(output=['HAMMER-RESULT: []', 'HAMMER-RESULT: {',
                                            'HAMMER-RESULT: {"status":"proved"}'])
        self.assertEqual(tool_info.Tool().determine_result(run), 'true')


class CollectorTests(unittest.TestCase):
    def test_benchexec_log_names(self):
        row = collector.parse_log("search20.000042-Foo.bar_baz'.yml.log",
                                  "\n".join(output({"status": "proved", "name": "Foo.bar_baz'"})))
        self.assertEqual((row["id"], row["rundefinition"], row["status"]), (42, "search20", "proved"))

    def test_killed_run_keeps_theorem_name(self):
        row = collector.parse_log("search5.000007-A.b.yml.log",
                                  "[run-hammer-task] theorem A.b; search 5s\n"
                                  "[vampire.profile] starting export\nHAMMER-RESULT: {\n")
        self.assertEqual((row["name"], row["status"]), ("A.b", "killed_by_limit"))
        self.assertEqual(row['last_phase'], 'export')
        self.assertIn('HAMMER-RESULT: {', row['log_tail'])

    def test_collector_recovers_full_multiline_errors_from_older_logs(self):
        message = 'Replay failed\n' + 'details ' * 100
        row = collector.parse_log('search5.000007-A.b.yml.log', '\n'.join([
            '/tmp/case.lean:2:0: error: replay error', '  offending term',
            *output({'name': 'A.b', 'status': 'error', 'failure_kind': 'replay_error', 'error': message})]))
        self.assertEqual(row['error'], message)
        self.assertTrue(row['lean_errors'][0].startswith('replay error\n  offending term'))

    def test_unrelated_logs_are_ignored(self):
        self.assertIsNone(collector.parse_log("benchexec.log", ""))


slurmbench = load("slurmbench", "slurmbench.py")


class SlurmbenchHammerTests(unittest.TestCase):
    def test_reification_category_and_case_scoped_attempt_events(self):
        slurmbench = load('slurmbench_events', 'slurmbench.py')
        event = {'event': 'deferred_witness_failed', 'name': 'T',
                 'initial_error': 'THF export: missing domain',
                 'terminal_error': 'Vampire timed out'}
        log = '\n'.join([
            '[vampire.event] ' + json.dumps(dict(event, name='Another')),
            '[vampire.event] ' + json.dumps(event),
            '[vampire.event] incomplete',
            'HAMMER-RESULT: ' + json.dumps({'name': 'T', 'status': 'error',
                'failure_kind': 'other_error',
                'error': 'Auto.LamReif.reifTermCheckType :: LamTerm is not type correct'})])
        metrics = slurmbench.hammer_metrics(log, 0)
        self.assertEqual(metrics['category'], 'reification_type_error')
        self.assertEqual(metrics['attempt-events'], [event])

    def test_baseline_metrics_and_collection_omit_diagnostics(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            cfg = {'output': str(out), 'name': 'comparison', 'tool': 'hammer_bench.tool_info',
                   'created': '2026-10-13T00:00:00Z', 'seconds': 180, 'mem_mb': 8192, 'cpus': 1,
                   'jobs': []}
            results = {}
            for idx, prover in enumerate(suite.PROVERS):
                job = {'input': '000000-T.thm', 'runset': f'{prover}-search10',
                       'args': ['--prover', prover, '--search-seconds', '10', '000000-T.thm']}
                cfg['jobs'].append(job)
                log = f'[run-hammer-task] theorem T; prover {prover}; search 10s\n'
                # Explicit prover also works for a run killed before its header.
                x = slurmbench.hammer_metrics(log, -9, timed_out=True, prover=prover)
                self.assertEqual(x['prover'], prover)
                self.assertEqual(x['status'], 'TIMEOUT')
                if prover != 'vampire-hammer':
                    self.assertTrue({'error-full', 'lean-errors', 'query-details', 'log-tail', 'phases'}.isdisjoint(x))
                    self.assertEqual(collector.failure_details(x), {})
                    self.assertEqual(slurmbench.hammer_metrics('', -9, prover=prover)['prover'], prover)
                results[idx] = {'idx': idx, 'status': x['status'], 'extracted': x, 'walltime': 180,
                                'returncode': -9, 'memory': 0, 'hostname': 'test'}
            manifest = out / 'manifest.json'
            manifest.write_text(json.dumps(cfg))
            with patch.object(slurmbench, 'read_results', return_value=results):
                slurmbench.collect(types.SimpleNamespace(manifest=str(manifest), partial=False))
            self.assertTrue((out / 'diagnostics/vampire-hammer-search10/failures.jsonl').exists())
            for prover in ('duper', 'aesop', 'simp_all'):
                self.assertFalse((out / 'diagnostics' / f'{prover}-search10').exists())
                xml = slurmbench.ET.parse(out / f'comparison.{prover}-search10.results.xml')
                titles = {column.attrib['title'] for column in xml.findall('columns/column')}
                self.assertTrue({'last-phase', 'error', 'queries', 'category'}.isdisjoint(titles))
            self.assertTrue((out / 'comparison-search10.json').exists())

    def test_result_reading_reports_progress_for_legacy_and_bulk(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            (output / 'logs').mkdir()
            slurmbench.save_json(output / 'logs' / '000000.json', {'idx': 0})
            local = output / 'local.log'
            local.write_text('log\n')
            journal = slurmbench.ResultJournal(output, 'test')
            journal.save({'idx': 1}, local)
            journal.close()
            with journal.records_path.open('ab') as stream:
                stream.write(b'{"idx":2')
            updates = []
            with patch.object(slurmbench.time, 'monotonic', side_effect=[0, 6, 6, 12, 12]):
                records = slurmbench.read_results(output, progress=updates.append)
            self.assertEqual(set(records), {0, 1})
            self.assertEqual(updates, [1, 2, 2])

    def test_resume_repeats_runs_without_usable_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            log = output / 'run.log'
            log.write_text('log\n')
            journal = slurmbench.ResultJournal(output, 'test')
            journal.save({'idx': 0, 'extracted': {'outcome': 'proved'}}, log)
            journal.save({'idx': 1, 'extracted': {'outcome': 'slurmbench_timeout'}}, log)
            journal.save({'idx': 2, 'extracted': {'outcome': 'no_result'}}, log)
            journal.save({'idx': 2, 'extracted': {'outcome': 'search_timeout'}}, log)
            journal.close()
            done = slurmbench.read_results(output, bulk_only=True)
            self.assertEqual(set(done), {0, 1, 2})
            # A rerun with --resume skips only the runs with a usable result;
            # the latest record wins, so a redo supersedes an earlier kill.
            self.assertEqual(slurmbench.usable_results(done), {0, 2})

    def test_bulk_journal_preserves_logs_and_ignores_interrupted_tail(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            local = output / 'local.log'
            journal = slurmbench.ResultJournal(output, 'shard-000-rank-000')
            for idx, text in [(0, 'first α\n'), (1, 'second\n')]:
                local.write_text(text)
                journal.save({'idx': idx, 'status': 'true'}, local)
            journal.close()
            with journal.records_path.open('ab') as stream:
                stream.write(b'{"idx":2')
            records = slurmbench.read_results(output, bulk_only=True)
            self.assertEqual(set(records), {0, 1})
            self.assertEqual(slurmbench.read_run_log(output, records[0]), 'first α\n')
            self.assertEqual(slurmbench.read_run_log(output, records[1]), 'second\n')
            self.assertEqual(set(slurmbench.read_results(output, True, wanted={1})), {1})
            newer = slurmbench.ResultJournal(output, 'shard-000-rank-000')
            local.write_text('replacement\n')
            newer.save({'idx': 0, 'status': 'unknown'}, local)
            newer.close()
            records = slurmbench.read_results(output, bulk_only=True)
            self.assertEqual(records[0]['status'], 'unknown')
            self.assertEqual(slurmbench.read_run_log(output, records[0]), 'replacement\n')
            copied = slurmbench.ResultJournal(output, 'packed-legacy')
            location = records[0]['log_storage']
            copied.save(dict(records[0]), output / location['path'], location['offset'],
                        location['length'], location['encoding'])
            copied.close()
            self.assertEqual(slurmbench.read_run_log(output, slurmbench.read_results(output, True)[0]), 'replacement\n')

    def test_bulk_execution_and_legacy_packing(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            (output / 'logs').mkdir()
            cfg = {'executable': sys.executable, 'jobs': [{'args': ['-c', 'print("hello")']}],
                   'tool': 'generic', 'workdir': temporary, 'output': temporary, 'seconds': 2,
                   'status_regex': '', 'name': 'fixture', 'created': '2026-10-12T00:00:00Z',
                   'mem_mb': 64, 'cpus': 1}
            cfg['jobs'][0].update(input='000000-fixture.thm', runset='default', taskset='tasks')
            slurmbench.run_one(cfg, 0)
            before = slurmbench.read_results(output)
            manifest = output / 'manifest.json'
            manifest.write_text(json.dumps(cfg))
            slurmbench.collect(types.SimpleNamespace(manifest=str(manifest), partial=False))
            xml_path = output / 'fixture.default.results.xml'
            before_xml = slurmbench.ET.parse(xml_path)
            slurmbench.pack_results(types.SimpleNamespace(manifest=str(manifest)))
            slurmbench.collect(types.SimpleNamespace(manifest=str(manifest), partial=False))
            after_xml = slurmbench.ET.parse(xml_path)
            self.assertEqual(before_xml.find('run').find('column').attrib,
                             after_xml.find('run').find('column').attrib)
            packed = slurmbench.read_results(output, bulk_only=True)
            self.assertEqual(slurmbench.read_run_log(output, packed[0]), 'hello\n')
            for key in ('status', 'returncode', 'extracted'):
                self.assertEqual(packed[0][key], before[0][key])
            self.assertTrue((output / 'logs/000000.json').exists())
            self.assertEqual(json.loads(manifest.read_text())['result_format'], 'bulk')
            cfg['jobs'].append({'args': ['-c', 'print("bulk")']})
            journal = slurmbench.ResultJournal(output, 'shard-001-rank-000')
            slurmbench.run_one(cfg, 1, journal=journal, local_log=output / 'slot.log')
            journal.close()
            self.assertFalse((output / 'logs/000001.json').exists())
            self.assertFalse((output / 'logs/000001.log').exists())
            self.assertEqual(slurmbench.read_run_log(output, slurmbench.read_results(output, True)[1]), 'bulk\n')

    def test_comparison_uses_common_eligible_theorems_and_reports_overlap(self):
        with tempfile.TemporaryDirectory() as temporary:
            completed = []
            for theorem in ('A', 'B', 'excluded'):
                for prover in suite.PROVERS:
                    solved = (theorem == 'A' and prover in {'vampire-hammer', 'aesop'}) or (theorem == 'B' and prover == 'duper')
                    data = {'status': 'true' if solved else 'unknown',
                            'extracted': {'theorem': theorem, 'outcome': 'excluded' if theorem == 'excluded' else 'proved' if solved else 'search_unsuccessful'}}
                    job = {'input': theorem, 'args': ['--prover', prover, '--search-seconds', '10']}
                    completed.append((job, data, len(completed)))
            completed.append(({'input': 'incomplete', 'args': ['--prover', 'vampire-hammer', '--search-seconds', '10']},
                              {'status': 'true', 'extracted': {'outcome': 'proved'}}, 12))
            slurmbench.comparison_diagnostics(Path(temporary), completed)
            report = json.loads((Path(temporary) / 'comparison-search10.json').read_text())
            self.assertEqual((report['common_completed'], report['common_eligible'], report['excluded'], report['incomplete']), (3, 2, 1, 1))
            self.assertEqual(report['proved'], {'vampire-hammer': 1, 'duper': 1, 'aesop': 1, 'simp_all': 0, 'grind': 0})
            self.assertEqual((report['proved_by_any'], report['proved_by_all']), (2, 0))
            self.assertEqual(report['only_this_prover']['duper'], 1)
            self.assertEqual(report['pairwise']['vampire-hammer']['aesop']['both'], 1)

    def test_partial_comparison_keeps_pairwise_denominators(self):
        with tempfile.TemporaryDirectory() as temporary:
            completed = []
            for theorem, outcomes in {
                'A': {'vampire-hammer': 'proved', 'duper': 'search_unsuccessful', 'aesop': 'proved'},
                'B': {'vampire-hammer': 'search_unsuccessful', 'duper': 'proved'},
                'excluded': {'vampire-hammer': 'excluded', 'duper': 'proved', 'aesop': 'excluded'},
            }.items():
                for prover, outcome in outcomes.items():
                    job = {'input': theorem, 'args': ['--prover', prover, '--search-seconds', '10']}
                    data = {'status': 'true' if outcome == 'proved' else 'unknown',
                            'extracted': {'theorem': theorem, 'outcome': outcome}}
                    completed.append((job, data, len(completed)))
            slurmbench.comparison_diagnostics(Path(temporary), completed)
            report = json.loads((Path(temporary) / 'comparison-search10.json').read_text())
            self.assertEqual(report['common_eligible'], 0)  # no simp_all results
            available = report['available_comparison']
            self.assertEqual(available['common_eligible'], 1)
            self.assertEqual(available['proved'], {'vampire-hammer': 1, 'duper': 0, 'aesop': 1})
            pairs = {tuple(row['provers']): row for row in report['pairwise_comparisons']}
            pair = pairs['vampire-hammer', 'duper']
            self.assertEqual((pair['common_completed'], pair['common_eligible'], pair['excluded']), (3, 2, 1))
            self.assertEqual(pair['proved'], {'vampire-hammer': 1, 'duper': 1})
            self.assertEqual(pair['proved_by_any'], 2)
            self.assertEqual(pair['proved_by_all'], 0)
            self.assertEqual(pairs['vampire-hammer', 'aesop']['proved_by_all'], 1)

    def test_repeated_arrays_and_statistics(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            task = base / 'task.txt'
            task.write_text('test')
            xml = base / 'suite.xml'
            xml.write_text('<benchmark command="/bin/true"><tasks><include>task.txt</include></tasks></benchmark>')
            ns = types.SimpleNamespace(benchmark=str(xml), output=str(base / 'results'),
                                       repetitions=3, partition='test', nodes=1, cpus_per_node=1,
                                       shards=2, slack=5, time=None, mem=None, sbatch_arg=None,
                                       scratch_root=None, job_array=False, array_parallel=None)
            with patch.object(slurmbench.subprocess, 'check_output', side_effect=['101', '102', '103']) as sbatch:
                slurmbench.submit_packed(ns)
            self.assertEqual(sbatch.call_count, 3)
            self.assertTrue(all('--array=0-1' in call.args[0] for call in sbatch.call_args_list))
            self.assertFalse(any(arg.startswith('--dependency=') for arg in sbatch.call_args_list[0].args[0]))
            self.assertIn('--dependency=afterany:101', sbatch.call_args_list[1].args[0])
            self.assertIn('--dependency=afterany:102', sbatch.call_args_list[2].args[0])
            manifest = base / 'results/manifest.json'
            cfg = json.loads(manifest.read_text())
            self.assertEqual(len(cfg['repetition_manifests']), 3)
            for i, path in enumerate(cfg['repetition_manifests']):
                child = json.loads(Path(path).read_text())
                self.assertEqual(child['job_ids'], [str(101 + i)])
                journal = slurmbench.ResultJournal(Path(child['output']), 'test')
                log = base / 'log'
                log.write_text('test')
                journal.save({'idx': 0, 'status': 'true' if i != 1 else 'unknown',
                              'walltime': i + 1, 'cputime': None, 'memory': None,
                              'returncode': 0, 'hostname': 'test'}, log)
                journal.close()
            slurmbench.collect(types.SimpleNamespace(manifest=str(manifest), partial=False))
            summary = json.loads((base / 'results/repetition-summary.json').read_text())
            stats = summary['runsets']['default']
            self.assertEqual(stats['proved_per_repetition'], [1, 0, 1])
            self.assertAlmostEqual(stats['proved']['mean'], 2 / 3)
            self.assertAlmostEqual(stats['proved']['stdev'], (1 / 3) ** .5)
            self.assertEqual(stats['proved_every_repetition'], 0)
            self.assertEqual(stats['proved_any_repetition'], 1)
            row = json.loads((base / 'results/repetition-cases.jsonl').read_text())
            self.assertEqual(row['metrics']['walltime']['mean'], 2)
            self.assertEqual(row['metrics']['walltime']['stdev'], 1)
            self.assertEqual(stats['total_seconds']['walltime']['per_repetition'], [1, 2, 3])
            with self.assertRaisesRegex(ValueError, 'already contains'):
                slurmbench.submit_packed(ns)
            ns.output = str(base / 'ordinary')
            ns.parallel = 2
            ns.sbatch_arg = ['--dependency=afterok:99']
            with patch.object(slurmbench.subprocess, 'check_output', side_effect=['201;cluster', '202;cluster', '203;cluster']) as sbatch:
                slurmbench.submit(ns)
            self.assertEqual(sbatch.call_count, 3)
            self.assertTrue(all('--array=0-0%2' in call.args[0] for call in sbatch.call_args_list))
            self.assertIn('--dependency=afterok:99,afterany:201', sbatch.call_args_list[1].args[0])
            self.assertIn('--dependency=afterok:99,afterany:202', sbatch.call_args_list[2].args[0])
            ns.repetitions = 0
            with self.assertRaisesRegex(ValueError, 'positive'):
                slurmbench.submit(ns)

    def test_repetition_partial_uses_common_eligible_cases(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            jobs = [{'runset': 'test', 'input': 'a'}, {'runset': 'test', 'input': 'b'}]
            manifests = []
            for i in range(3):
                path = out / f'{i}.json'
                slurmbench.save_json(path, {'jobs': jobs, 'output': str(out)})
                manifests.append(str(path))
            good = {'status': 'true', 'walltime': 1}
            cfg = {'output': str(out), 'repetition_manifests': manifests}
            samples = [{0: good, 1: good}, {0: good},
                       {0: good, 1: {'status': 'unknown', 'extracted': {'outcome': 'excluded'}}}]
            with patch.object(slurmbench, 'collect'), patch.object(slurmbench, 'read_results', side_effect=samples):
                slurmbench.collect_repeated(types.SimpleNamespace(partial=True), cfg)
            report = json.loads((out / 'repetition-summary.json').read_text())
            self.assertFalse(report['complete'])
            self.assertEqual(report['runsets']['test']['common_eligible_cases'], 1)
            self.assertEqual(report['runsets']['test']['proved_per_repetition'], [1, 1, 1])
            self.assertIsNone(slurmbench.repeat_statistics([1])['stdev'])
            with patch.object(slurmbench, 'collect'), patch.object(slurmbench, 'read_results', side_effect=samples):
                with self.assertRaisesRegex(ValueError, 'unusable'):
                    slurmbench.collect_repeated(types.SimpleNamespace(partial=False), cfg)

    def test_packed_array_submission_dispatch_and_selective_resubmission(self):
        with tempfile.TemporaryDirectory(prefix='array test ') as temporary:
            base = Path(temporary)
            cfg = {'tool': 'hammer_bench.tool_info', 'cpus': 1, 'mem_mb': 8000, 'seconds': 180,
                   'jobs': [{} for _ in range(12)]}
            ns = types.SimpleNamespace(benchmark=str(base / 'suite.xml'), output=str(base / 'results'),
                                       partition='CPU-amd', nodes=1, cpus_per_node=4, shards=3,
                                       slack=5, time=None, mem='32G', sbatch_arg=None,
                                       scratch_root='/scratch', job_array=True, array_parallel=2,
                                       require_no_boost_no_smt=True)
            with patch.object(slurmbench, 'parse_xml', return_value=cfg), \
                    patch.object(slurmbench.subprocess, 'check_output', return_value='123\n') as sbatch:
                slurmbench.submit_packed(ns)
            sbatch.assert_called_once()
            command = sbatch.call_args.args[0]
            self.assertIn('--array=0-2%2', command)
            self.assertIn('--time=8', command)  # one round per element, not per concurrent slot
            self.assertTrue(any(arg.endswith('/%A_%a.out') for arg in command))
            manifest = base / 'results/manifest.json'
            saved = json.loads(manifest.read_text())
            self.assertTrue(saved['require_no_boost_no_smt'])
            self.assertTrue(saved['physical_cores'])
            self.assertEqual(saved['scratch_root'], '/scratch')
            self.assertEqual(set(saved['shard_commands']), {'0', '1', '2'})
            shard_script = base / 'results/slurm/launch-001.sh'
            self.assertIn('packed-worker', shard_script.read_text())
            resubmit = types.SimpleNamespace(manifest=str(manifest), shard=1, sbatch_arg=None)
            with patch.object(slurmbench.subprocess, 'check_output', return_value='124\n') as sbatch:
                slurmbench.resubmit_shard(resubmit)
            command = sbatch.call_args.args[0]
            self.assertEqual([arg for arg in command if arg.startswith('--array=')], ['--array=1'])
            self.assertIn('--resume', shard_script.read_text())
            self.assertIn('--kill-on-bad-exit=1', shard_script.read_text())
            # Exercise shell quoting and actual array-ID dispatch without Slurm.
            shard_script.write_text('#!/bin/bash\necho selected-shard-1\n')
            selected = subprocess.check_output(['bash', str(base / 'results/slurm/launch-array.sh')],
                                               env={**os.environ, 'SLURM_ARRAY_TASK_ID': '1'}, text=True)
            self.assertEqual(selected.strip(), 'selected-shard-1')

    @unittest.skipUnless(shutil.which('rsync'), 'rsync required for node-local staging')
    def test_scratch_staging_relocates_runtime_paths_and_preserves_results(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project, toolchain = base / 'project', base / 'toolchain'
            scratch = base / 'scratch'
            scratch.mkdir()
            files = [project / 'slurmrunner/run-hammer-task', project / '.lake/build/lib/lean/Hammer.olean',
                     project / 'vampire', project / 'parser.so', toolchain / 'bin/lean',
                     project / 'Mathlib.lean', project / '.lake/build/lib/libhammer.so',
                     toolchain / 'lib/lean/libLake_shared.so']
            for path in files:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(path.name)
            # Linked dependencies must become real node-local files.
            dependency = base / 'dependency'
            dependency.mkdir()
            (dependency / 'Dependency.olean').write_text('dependency')
            (project / 'linked-dependency').symlink_to(dependency, target_is_directory=True)
            config = {'project': str(project), 'lean': str(toolchain / 'bin/lean'),
                      'vampire': str(project / 'vampire'), 'tptp_parser_dynlib': str(project / 'parser.so'),
                      'lean_env': {'LEAN_PATH': os.pathsep.join([
                          '.lake/build/lib/lean', '.lake/packages/Cli/.lake/build/lib/lean',
                          str(base / 'missing-external/lib/lean')]), 'LEAN_SRC_PATH': str(project),
                                   'LD_LIBRARY_PATH': str(toolchain / 'bin')},
                      'lean_args': [f"--load-dynlib={project / 'parser.so'}",
                                    f"--plugin={toolchain / 'lib/lean/libLake_shared.so'}",
                                    f"--plugin={project / '.lake/build/lib/libhammer.so'}"],
                      'vampire_sha256': 'fingerprint', 'evaluation_strategy': 'lean-auto-style'}
            suite_config = base / 'suite-config.json'
            suite_config.write_text(json.dumps(config))
            task = str(base / 'task.thm')
            cfg = {'tool': 'hammer_bench.tool_info', 'workdir': str(base), 'output': str(base / 'results'),
                   'executable': str(files[0]), 'jobs': [
                       {'input': task, 'args': ['--config', str(suite_config), task]},
                       {'input': task, 'args': ['--config', str(suite_config), '--search-seconds', '5', task]}]}
            staged, root = slurmbench.stage_hammer(cfg, scratch)
            local_config_path = Path(staged['jobs'][0]['args'][1])
            local = json.loads(local_config_path.read_text())
            self.assertEqual(staged['output'], cfg['output'])
            self.assertEqual(staged['jobs'][0]['input'], task)
            self.assertEqual(staged['jobs'][1]['args'][1], str(local_config_path))
            self.assertEqual(cfg['jobs'][0]['args'][1], str(suite_config))
            self.assertEqual(local['vampire_sha256'], 'fingerprint')
            self.assertEqual(local['evaluation_strategy'], 'lean-auto-style')
            self.assertEqual(Path(staged['executable']).read_text(), 'run-hammer-task')
            self.assertEqual(Path(local['lean']).read_text(), 'lean')
            search_paths = [Path(part) for part in local['lean_env']['LEAN_PATH'].split(os.pathsep)]
            self.assertTrue(all(path.is_relative_to(root) for path in search_paths))
            self.assertTrue(search_paths[0].is_dir())
            self.assertFalse(search_paths[1].exists())
            self.assertFalse(search_paths[2].exists())
            self.assertEqual(len(list(root.glob('tree-*'))), 2)
            self.assertTrue(Path(staged['env_overrides']['TMPDIR']).is_dir())
            shutil.rmtree(project)
            shutil.rmtree(toolchain)
            shutil.rmtree(dependency)
            copied = Path(local['project']) / 'linked-dependency'
            self.assertFalse(copied.is_symlink())
            self.assertEqual((copied / 'Dependency.olean').read_text(), 'dependency')
            self.assertEqual(Path(local['vampire']).read_text(), 'vampire')
            # The native-library flags point into the node-local copies.
            self.assertEqual([flag.split('=', 1)[0] for flag in local['lean_args']],
                             ['--load-dynlib', '--plugin', '--plugin'])
            libraries = [Path(flag.split('=', 1)[1]) for flag in local['lean_args']]
            self.assertTrue(all(path.is_relative_to(root) for path in libraries))
            self.assertEqual([path.read_text() for path in libraries],
                             ['parser.so', 'libLake_shared.so', 'libhammer.so'])

    def test_xml_tool_selects_hammer_mode(self):
        self.assertEqual(slurmbench.tool_kind({"tool": "hammer_bench.tool_info"}), "hammer")
        self.assertEqual(slurmbench.tool_kind({"tool": "vampire"}), "vampire")

    def test_satisfiable_search(self):
        row = {"name": "T", "status": "error", "failure_kind": "search_unsuccessful", "szs_status": "Satisfiable",
               "error": "Vampire did not produce a refutation", "seconds": 1.0, "wall_seconds": 5.0, "route": "vampire"}
        x = slurmbench.hammer_metrics("\n".join(output(row)), 0)
        self.assertEqual((x["status"], x["category"], x["startup-seconds"]), ("unknown", "satisfiable", 4.0))

    def test_heartbeat_abort_and_unfinished_phase(self):
        log = "\n".join([
            "[run-hammer-task] theorem T; search 20s",
            "/tmp/h/case.lean:9:0: error: (deterministic) timeout at `whnf`, maximum number of heartbeats (200000) has been reached",
            "[vampire.profile] starting source fact collection and lowering",
            "[vampire.profile] 284 ms: source fact collection and lowering",
            "[vampire.profile] starting reification, preprocessing and TF0 export",
            "HAMMER-RESULT: " + json.dumps({"name": "T", "status": "process_error", "failure_kind": "process_error"})])
        x = slurmbench.hammer_metrics(log, 0)
        self.assertEqual((x["status"], x["category"]), ("ERROR (process_error)", "heartbeat_limit"))
        self.assertEqual(x["last-phase"], "reification, preprocessing and TF0 export")
        self.assertAlmostEqual(x["phases"]["source fact collection and lowering"], 0.284)

    def test_full_failure_details_survive_compact_summary(self):
        message = 'Unsupported expression\n' + 'long expression ' * 100
        query = {'problem': '00000.p', 'stderr_tail': 'Initialization failure'}
        log = '\n'.join([
            '/tmp/path with spaces/case.lean:9:0: error: first line',
            '  continuation',
            '[vampire.profile] starting export',
            'HAMMER-RESULT: ' + json.dumps({'name': 'T', 'status': 'error',
                'failure_kind': 'unsupported_input', 'error': message, 'query_details': [query]})])
        x = slurmbench.hammer_metrics(log, 0)
        self.assertEqual(x['error-full'], message)
        self.assertEqual(x['lean-errors'], ['first line\n  continuation'])
        self.assertEqual(x['query-details'], [query])
        self.assertLessEqual(len(x['error']), 300)
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            data = {'idx': 1, 'status': x['status'], 'extracted': x, 'walltime': 3,
                    'returncode': 0, 'log_storage': 'bulk/logs-1.tar.gz#000001.log'}
            job = {'input': 'theorems.jsonl#000001-T'}
            slurmbench.hammer_diagnostics(out, 'hammer', [(job, data, None)])
            failure = json.loads((out / 'diagnostics/hammer/failures.jsonl').read_text())
            self.assertEqual(failure['error-full'], message)
            self.assertEqual(failure['query-details'], [query])
            self.assertEqual(failure['log'], data['log_storage'])

    def test_killed_runs(self):
        log = "[run-hammer-task] theorem T; search 20s\n[vampire.profile] starting source rewriting"
        timeout = slurmbench.hammer_metrics(log, -9, timed_out=True)
        self.assertEqual((timeout["status"], timeout["theorem"], timeout["last-phase"]),
                         ("TIMEOUT", "T", "source rewriting"))
        self.assertEqual(slurmbench.hammer_metrics(log, -9)["status"], "ERROR (killed_by_signal_9)")

    def test_proof_and_unsupported(self):
        proved = slurmbench.hammer_metrics("\n".join(output({"status": "proved", "failure_kind": "proved"})), 0)
        self.assertEqual(proved["status"], "true")
        late = slurmbench.hammer_metrics("\n".join(output({"status": "proof_timeout", "failure_kind": "proof_timeout"})), 0)
        self.assertEqual(late["status"], "unknown")
        unsupported = {"status": "error", "failure_kind": "unsupported_input",
                       "error": "Auto.Inhabitation: type is not inhabited"}
        x = slurmbench.hammer_metrics("\n".join(output(unsupported)), 0)
        self.assertEqual((x["status"], x["category"]), ("unknown", "missing_domain_witness"))

    def test_successful_reconstruction_modes_are_reported_from_accepted_proof(self):
        event = {'name': 'T', 'status': 'proved', 'route': 'vampire', 'route_source': 'accepted_proof',
                 'reconstruction_modes': ['fo', 'ho'], 'kernel_checked': True}
        x = slurmbench.hammer_metrics('\n'.join(output(event)), 0)
        self.assertEqual(x['route-source'], 'accepted_proof')
        self.assertEqual(x['reconstruction'], 'fo,ho')
        self.assertEqual(x['reconstruction-modes'], ['fo', 'ho'])
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            slurmbench.hammer_diagnostics(out, 'hammer', [({'input': 'T'},
                {'idx': 0, 'status': 'true', 'extracted': x, 'walltime': 1}, None)])
            summary = json.loads((out / 'diagnostics/hammer/summary.json').read_text())
            self.assertEqual(summary['proved_by_reconstruction'], {'fo,ho': 1})


if __name__ == "__main__":
    unittest.main()
