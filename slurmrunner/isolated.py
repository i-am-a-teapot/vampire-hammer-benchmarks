"""Per-prover Lake environments for the shared suite generator and runner."""
from contextlib import contextmanager
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def clean_environment():
    """Do not inherit module paths from another active Lake workspace."""
    return {key: value for key, value in os.environ.items()
            if key not in {'LEAN_PATH', 'LEAN_SRC_PATH', 'LEAN_SYSROOT'}}


@contextmanager
def project_context(namespace, project):
    benchmark = namespace['benchmark']
    previous = namespace['PROJECT'], namespace['LAKE'], benchmark.PROJECT, benchmark.LAKE
    namespace['PROJECT'], namespace['LAKE'] = project, ['lake']
    benchmark.PROJECT, benchmark.LAKE = project, ['lake']
    try:
        yield
    finally:
        namespace['PROJECT'], namespace['LAKE'], benchmark.PROJECT, benchmark.LAKE = previous


def load_backends(path, selected=None):
    path = Path(path).resolve()
    registry = json.loads(path.read_text())
    if registry.get('version') != 1 or not registry.get('backends'):
        raise ValueError('expected a version 1 registry with nonempty backends')
    backends = registry['backends']
    selected = list(dict.fromkeys(selected or backends))
    unknown = set(selected) - set(backends)
    if unknown:
        raise ValueError(f'unknown backends: {sorted(unknown)}')
    result = {}
    for name in selected:
        if not name or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789_-' for c in name):
            raise ValueError(f'invalid backend name: {name!r}')
        spec = dict(backends[name])
        project = Path(spec['project']).expanduser()
        spec['project'] = str((path.parent / project).resolve())
        if not (Path(spec['project']) / 'lakefile.lean').is_file():
            raise ValueError(f'backend {name} has no Lake project: {spec["project"]}')
        result[name] = spec
    return result


def backend_config(name, spec, args, namespace):
    benchmark = namespace['benchmark']
    project = Path(spec['project'])
    adapter = spec.get('adapter_module', 'Benchmark.Prover')
    imports = spec.get('benchmark_imports', f'import Mathlib\nimport {adapter}\n')
    if not all(part.isidentifier() for part in adapter.split('.')):
        raise ValueError(f'invalid adapter module: {adapter!r}')
    with project_context(namespace, project):
        probe = subprocess.run(['lake', 'build', '--no-build', adapter], cwd=project,
                               capture_output=True, text=True, env=clean_environment())
        if probe.returncode:
            raise ValueError(f'build backend {name} first in {project}:\n{probe.stdout}{probe.stderr}')
        lean, environment = namespace['lean_environment'](clean=True)
        flags = namespace['native_lean_args'](imports=imports, env=clean_environment())
        mathlib = benchmark.package_root('mathlib', project)
        mathlib_revision = subprocess.check_output(['git', '-C', str(mathlib), 'rev-parse', 'HEAD'], text=True).strip()
        lean_version = subprocess.check_output([lean, '--version'], text=True).strip()
        manifest = json.loads((project / 'lake-manifest.json').read_text())
        config = {
            'project': str(project), 'lean': lean, 'lean_env': environment, 'lean_args': flags,
            'lean_version': lean_version, 'mathlib_commit': mathlib_revision,
            'benchmark_imports': imports, 'adapter_module': adapter,
            'provers': [name], 'diagnostic_provers': [], 'isolated_backend': True,
            'dependency_revisions': {p['name']: p.get('rev', p.get('dir')) for p in manifest['packages']},
            'evaluation_strategy': args.strategy, 'comparison': args.comparison,
            'success_seconds': args.success_seconds,
            'proof_timeout_seconds': args.success_seconds,
            'search_seconds': args.search_seconds[0],
            'case_options': [*spec.get('case_options', []), *args.case_option],
            'artifacts': spec.get('artifacts'),
            'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        # Optional external-solver artifacts are backend capabilities, not a
        # reason to change how the backend is configured, built or scheduled.
        if spec.get('artifacts') == 'vampire':
            binary = Path(args.vampire).expanduser().resolve() if args.vampire else benchmark.vampire_executable(project)
            if not os.access(binary, os.X_OK):
                raise ValueError(f'backend {name}: build its solver or pass --vampire; missing {binary}')
            config.update(vampire=str(binary), vampire_sha256=benchmark.binary_hash(binary),
                          vampire_memory_mib=args.vampire_memory_mib,
                          tptp_parser_dynlib=str(namespace['parser_dynlib']()),
                          hammer_project=str(benchmark.hammer_project(project)),
                          hammer_revision=namespace['hammer_revision'](), diagnostic_provers=[name])
        files = [project / 'lakefile.lean', project / 'lake-manifest.json', project / 'lean-toolchain',
                 project / (adapter.replace('.', '/') + '.lean')]
        config['backend_source_hashes'] = {
            str(path.relative_to(project)): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
        return config


def make_suite(args, namespace):
    benchmark = namespace['benchmark']
    backends = load_backends(args.backends, args.prover)
    output = args.output.resolve()
    if output.exists():
        raise ValueError(f'output already exists: {output}')
    configs = {name: backend_config(name, spec, args, namespace) for name, spec in backends.items()}
    if len({c['lean_version'] for c in configs.values()}) != 1:
        raise ValueError('all backends must use the same Lean version')
    if len({c['mathlib_commit'] for c in configs.values()}) != 1:
        raise ValueError('all backends must use the same Mathlib revision')
    output.mkdir(parents=True)
    rows, eligible = namespace['select'](args, output)
    if not rows:
        raise ValueError('the theorem selection is empty')
    # Slurm uses the same XML and runner for every backend; the only varying
    # argument is the config identifying its independent Lean process.
    args.backend_configs = {}
    for name, config in configs.items():
        filename = output / f'{name}-config.json'
        args.backend_configs[name] = filename
        config['selection'] = {'count': len(rows), 'eligible_theorems': eligible}
        config['methodology'] = {'source_replay': args.strategy == 'lean-auto-style',
                                 'environment': 'before-declaration-command' if args.strategy == 'lean-auto-style'
                                                else 'full-import-with-target-reuse-audit',
                                 'same_imports_for_all_provers': False,
                                 'isolated_prover_projects': True, 'heartbeat_limit': 0}
        config['process_wall_seconds'] = namespace['duration_seconds'](args.timelimit)
        benchmark.write_json(filename, config)
        for relative in config['backend_source_hashes']:
            destination = output / 'source-snapshot' / name / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(Path(config['project']) / relative, destination)
    # Record the exact shared core sources, not just backend adapters.
    core_files = [namespace['TOOL_PROJECT'] / name for name in (
        'lakefile.lean', 'lake-manifest.json', 'lean-toolchain',
        'VampireHammerBenchmark/Mathlib.lean', 'VampireHammerBenchmark/Comparison.lean',
        'VampireHammerBenchmark.lean', 'slurmrunner/prepare-provers.py',
        'slurmrunner/isolated.py', 'slurmrunner/make-suite.py', 'slurmrunner/run-hammer-task',
        'slurmrunner/slurmbench.py', 'scripts/benchmark-mathlib.py')]
    hashes = {}
    for path in core_files:
        relative = path.relative_to(namespace['TOOL_PROJECT'])
        destination = output / 'source-snapshot/core' / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        hashes[str(relative)] = hashlib.sha256(path.read_bytes()).hexdigest()
    benchmark.write_json(output / 'source-hashes.json', hashes)
    for name, config in configs.items():
        config['shared_core_project'] = str(namespace['TOOL_PROJECT'])
        config['shared_core_source_hashes'] = hashes
        benchmark.write_json(args.backend_configs[name], config)
    summary = {'provers': list(configs), 'backend_configs': {name: str(path) for name, path in args.backend_configs.items()},
               'selection': {'count': len(rows), 'eligible_theorems': eligible},
               'isolated_prover_projects': True}
    benchmark.write_json(output / 'suite-config.json', summary)
    (output / 'proves.prp').write_text('An independent kernel-checked proof without sorry or target reuse.\n')
    shards = namespace['write_tasks'](rows, output, args.shard_size, compact=args.compact)
    (output / 'vampire-hammer.xml').write_text(namespace['benchmark_xml'](args, output / 'suite-config.json', shards))
    print(f'Wrote {len(rows)} tasks for {len(configs)} isolated backends to {output}')
    return 0


def local_main(argv, project):
    """Convenience entry point; use exactly the suite generator and Slurm worker."""
    parser = argparse.ArgumentParser(description='Run isolated prover backends locally. '
                                    'Additional options are passed to make-suite.py.', add_help=False)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--jobs', type=int, default=4)
    parser.add_argument('--batch-by-module', '--chunk-size', dest='batch_size', type=int, default=1)
    parser.add_argument('--extract-only', action='store_true', help='prepare the suite without executing tasks')
    parser.add_argument('--resume', action='store_true')
    local, options = parser.parse_known_args(argv)
    generator = project / 'slurmrunner/make-suite.py'
    if '--help' in options:
        print(parser.format_help())
        return subprocess.run([sys.executable, str(generator), '--help']).returncode
    if local.output is None:
        parser.error('--output is required with --backends')
    if local.jobs <= 0 or local.batch_size <= 0:
        parser.error('--jobs and --batch-by-module must be positive')
    output = local.output.resolve()
    suite = output / 'suite'
    if not local.resume:
        if output.exists():
            parser.error(f'output already exists: {output}')
        output.mkdir(parents=True)
        command = [sys.executable, str(generator), *options, '--compact', '--output', str(suite)]
        code = subprocess.run(command).returncode
        if code:
            return code
    if local.extract_only:
        return 0
    command = [sys.executable, str(project / 'slurmrunner/slurmbench.py'), 'run-local',
               str(suite / 'vampire-hammer.xml'), '-o', str(output / 'results'),
               '--jobs', str(local.jobs), '--batch-by-module', str(local.batch_size)]
    if local.resume and (output / 'results/manifest.json').exists():
        command.append('--resume')
    return subprocess.run(command).returncode
