#!/usr/bin/env python3
"""Build a pinned prover environment in a persistent volume, then run the sample."""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET


def positive(value):
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError('must be positive')
    return result


def fingerprint(source, definition):
    """Changed image sources or mounted prover definitions need a new workspace."""
    digest = hashlib.sha256()
    paths = sorted(path for path in source.rglob('*') if path.is_file())
    tree = ET.parse(definition).getroot()
    if tree.tag == 'prover-environment':
        paths += [(definition.parent / child.attrib['file']).resolve() for child in tree]
    elif tree.tag != 'prover':
        raise ValueError('prover definition must be <prover-environment> or <prover>')
    paths.append(definition)
    for path in paths:
        label = str(path.relative_to(source)) if path.is_relative_to(source) else str(path)
        digest.update(label.encode() + b'\0' + path.read_bytes() + b'\0')
    return digest.hexdigest()


def copy_changed(source, destination):
    destination = Path(destination)
    if not destination.is_file() or Path(source).read_bytes() != destination.read_bytes():
        shutil.copy2(source, destination)
    return str(destination)


def prepare(source, state, definition, jobs):
    revision = fingerprint(source, definition)
    root = state / 'workspaces' / revision[:20]
    root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, root, dirs_exist_ok=True, copy_function=copy_changed)
    if definition.is_relative_to(source):
        definition = root / definition.relative_to(source)
    catalog = root / 'artifacts/catalog.jsonl'
    stamp = root / '.prepared.json'
    if stamp.exists() and json.loads(stamp.read_text()).get('fingerprint') == revision \
            and catalog.is_file() and (root / 'artifacts/provers/backends.json').is_file():
        print(f'Reusing compiled environment: {root}', flush=True)
        return root, catalog
    environment = {key: value for key, value in os.environ.items()
                   if key not in {'LEAN_PATH', 'LEAN_SRC_PATH', 'LEAN_SYSROOT'}}
    environment.update(ELAN_HOME=str(state / 'elan'), MATHLIB_NO_CACHE_ON_UPDATE='1',
                       LEAN_NUM_THREADS=str(jobs))
    print(f'Compiling from source in {root} (Lean build threads: {jobs}).', flush=True)
    print('A full Mathlib build can take a long time. Reruns resume this workspace.', flush=True)
    # The committed manifest fetches the exact locked revisions. Do not run
    # `lake update` here, which could advance dependencies with moving refs.
    commands = [
        ['lake', '--no-cache', 'build', 'Mathlib:shared', 'VampireHammerBenchmark'],
        [sys.executable, 'scripts/prepare-provers.py', '--definition', str(definition),
         '--output', 'artifacts/provers', '--build', '--no-cache', '--build-jobs', str(jobs)],
    ]
    for command in commands:
        subprocess.run(command, cwd=root, env=environment, check=True)
    catalog.parent.mkdir(exist_ok=True)
    catalog_source = root / 'artifacts/Catalog.lean'
    catalog_source.write_text('import Mathlib\nimport VampireHammerBenchmark.Mathlib\n\n'
                              'set_option maxHeartbeats 0\n'
                              f'hammer_mathlib_proof_catalog {json.dumps(str(catalog), ensure_ascii=False)}\n')
    subprocess.run(['lake', '--no-build', 'lean', str(catalog_source)],
                   cwd=root, env=environment, check=True)
    temporary = stamp.with_suffix('.tmp')
    temporary.write_text(json.dumps({'fingerprint': revision, 'definition': str(definition)}) + '\n')
    temporary.replace(stamp)
    return root, catalog


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument('--prepare-only', action='store_true', help='compile and cache the environment without benchmarking')
    parser.add_argument('--prover-definition', type=Path,
                        default=os.environ.get('BENCHMARK_PROVERS', 'configurations/provers_sample.xml'),
                        help='prover environment XML (relative to the image sources, or an absolute mounted path)')
    parser.add_argument('--build-jobs', type=positive,
                        default=os.environ.get('BENCHMARK_BUILD_JOBS', '2'), help='Lean build threads and Vampire compile jobs')
    args, benchmark_args = parser.parse_known_args(argv)
    source = Path(os.environ.get('BENCHMARK_SOURCE', '/opt/benchmark-src')).resolve()
    if '--help' in benchmark_args or '-h' in benchmark_args:
        print(parser.format_help(), flush=True)
        return subprocess.run([sys.executable, str(source / 'docker/entrypoint.py'), '--help']).returncode
    # Check benchmark options before starting a potentially lengthy build.
    spec = importlib.util.spec_from_file_location(
        'sample_entrypoint', source / 'docker/entrypoint.py')
    entrypoint = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entrypoint)
    entrypoint.parse_args(benchmark_args)
    state = Path(os.environ.get('BENCHMARK_STATE', '/var/lib/benchmark')).resolve()
    state.mkdir(parents=True, exist_ok=True)
    try:
        lock = (state / '.lock').open('a')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error('the build volume is in use by another container; use a separate volume')
    # Keep the lock through both execs and the benchmark, with no supervising
    # process in BenchExec's cgroup. It is released when the container exits.
    os.set_inheritable(lock.fileno(), True)
    definition = (source / args.prover_definition.expanduser()).resolve()
    try:
        root, catalog = prepare(source, state, definition, args.build_jobs)
    except (OSError, ValueError, KeyError, ET.ParseError, subprocess.CalledProcessError) as error:
        parser.error(str(error))
    if args.prepare_only:
        return 0
    environment = dict(os.environ, ELAN_HOME=str(state / 'elan'),
                       BENCHMARK_ROOT=str(root), BENCHMARK_CATALOG=str(catalog))
    definition = Path(environment.get('BENCHMARK_DEFINITION', 'configurations/normal_small.xml'))
    environment['BENCHMARK_DEFINITION'] = str(root / definition)
    os.chdir(root)
    os.execvpe(sys.executable, [sys.executable, str(root / 'docker/entrypoint.py'), *benchmark_args], environment)


if __name__ == '__main__':
    sys.exit(main())
