#!/usr/bin/env python3
"""Create independent Lake projects for the common on-the-fly benchmark runner."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET

PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_DEFINITION = PROJECT / 'configurations/provers.xml'


def load_definition(path):
    """Read an environment index or a single prover definition without executing it."""
    def parse(source):
        try:
            return ET.parse(source).getroot()
        except (OSError, ET.ParseError) as error:
            raise ValueError(f'{source}: {error}') from error

    def attributes(element, allowed, required=()):
        if set(element.attrib) - allowed or set(required) - element.attrib.keys():
            raise ValueError(f'invalid attributes on <{element.tag}>: {element.attrib}')

    path = Path(path).expanduser().resolve()
    root = parse(path)
    if root.tag == 'prover-environment':
        attributes(root, {'version'}, {'version'})
        if root.get('version') != '1':
            raise ValueError('expected a version 1 prover environment')
        paths = []
        for child in root:
            if child.tag != 'prover' or len(child):
                raise ValueError('an environment contains <prover file="..."/> entries')
            attributes(child, {'file'}, {'file'})
            paths.append((path.parent / child.get('file')).resolve())
    elif root.tag == 'prover':
        paths = [path]
    else:
        raise ValueError('expected <prover-environment> or <prover>')
    backends = {}
    checkouts = []
    for source in paths:
        prover = parse(source)
        if prover.tag != 'prover':
            raise ValueError(f'{source}: expected <prover>')
        attributes(prover, {'name', 'checkout', 'artifacts'}, {'name'})
        name = prover.get('name')
        if not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', name) or name in backends:
            raise ValueError(f'invalid or duplicate prover name: {name!r}')
        checkout = Path(prover.get('checkout', name))
        if checkout.is_absolute() or not checkout.parts or '..' in checkout.parts or checkout.parts[0] in {
                'backends.json', 'environment.xml', 'definitions'}:
            raise ValueError(f'checkout must be a relative project directory: {checkout}')
        if any(checkout.is_relative_to(other) or other.is_relative_to(checkout) for other in checkouts):
            raise ValueError(f'overlapping prover checkout directories: {checkout}')
        checkouts.append(checkout)
        if prover.get('artifacts') not in {None, 'vampire'}:
            raise ValueError(f'unsupported solver artifacts: {prover.get("artifacts")}')
        spec = {'checkout': str(checkout), 'artifacts': prover.get('artifacts'),
                'dependencies': [], 'definition': source}
        singleton = set()
        dependency_names = set()
        for child in prover:
            if len(child):
                raise ValueError(f'nested elements are not supported in <{child.tag}>')
            if child.tag in {'imports', 'declarations', 'wrapper'}:
                attributes(child, set())
                if child.tag in singleton:
                    raise ValueError(f'duplicate <{child.tag}> in {source}')
                singleton.add(child.tag)
                spec[child.tag] = (child.text or '').strip() + '\n'
            elif child.tag == 'dependency':
                attributes(child, {'name', 'git', 'commit', 'path'}, {'name', 'git', 'commit'})
                dependency = dict(child.attrib)
                package = dependency['name']
                if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', package) or package in dependency_names:
                    raise ValueError(f'invalid or duplicate dependency: {package!r}')
                if package == 'vampire_hammer_benchmark':
                    raise ValueError('the harness dependency is supplied by the root project')
                dependency_names.add(package)
                if not dependency['git'].strip() or not re.fullmatch(r'(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})', dependency['commit']):
                    raise ValueError(f'{package}: git URL and full commit hash are required')
                if 'path' in dependency:
                    dependency['path'] = str((source.parent / Path(dependency['path']).expanduser()).resolve())
                spec['dependencies'].append(dependency)
            else:
                raise ValueError(f'unsupported prover element: <{child.tag}>')
        if not spec.get('wrapper', '').strip():
            raise ValueError(f'{source}: a Lean <wrapper> expression is required')
        spec.setdefault('imports', '')
        spec.setdefault('declarations', '')
        backends[name] = spec
    if not backends:
        raise ValueError('the prover environment is empty')
    return backends


def dependency_requires(spec, directory, shared):
    lines = []
    for dependency in spec['dependencies']:
        name, url, commit = dependency['name'], dependency['git'], dependency['commit']
        if name in shared:
            baseline = shared[name]
            if url != baseline['url'] or commit != baseline['revision'] or 'path' in dependency:
                raise ValueError(f'{name}: cannot override the shared Mathlib dependency pin')
            continue
        if 'path' in dependency:
            checkout = Path(dependency['path'])
            if not any((checkout / filename).is_file() for filename in ('lakefile.lean', 'lakefile.toml')):
                raise ValueError(f'{name}: no Lake project in local checkout {checkout}')
            revision = subprocess.check_output(['git', '-C', str(checkout), 'rev-parse', 'HEAD'], text=True).strip()
            if revision.lower() != commit.lower():
                raise ValueError(f'{name}: local checkout does not match commit {commit}')
            dirty = subprocess.check_output(
                ['git', '-C', str(checkout), 'status', '--porcelain', '--untracked-files=no'], text=True).strip()
            if dirty:
                raise ValueError(f'{name}: local checkout has modified tracked files')
            lines.append(f'require {name} from {json.dumps(os.path.relpath(checkout, directory))}\n')
        else:
            lines.append(f'require {name} from git {json.dumps(url)} @ {json.dumps(commit)}\n')
    return ''.join(lines)


def shared_dependencies():
    """Use the root's pinned Mathlib closure, including its native build cache."""
    manifest = json.loads((PROJECT / 'lake-manifest.json').read_text())
    packages = {}
    for package in manifest['packages']:
        directory = (PROJECT / package['dir'] if package['type'] == 'path' else
                     PROJECT / manifest.get('packagesDir', '.lake/packages') / package['name'])
        packages[package['name']] = {'project': str(directory.resolve()), 'revision': package.get('rev'),
                                     'url': package.get('url')}
    return packages


def prepare(output, provers=None, build=False, definition=DEFAULT_DEFINITION):
    backends = load_definition(definition)
    provers = list(dict.fromkeys(backends if provers is None else provers))
    if not provers or set(provers) - backends.keys():
        raise ValueError(f'unknown or empty prover selection: {provers}')
    output = output.resolve()
    # Preflight all destinations before creating any projects.
    for prover in provers:
        directory = output / backends[prover]['checkout']
        if directory.exists() or directory.is_symlink():
            raise ValueError(f'project already exists: {directory}')
        if not directory.resolve().is_relative_to(output):
            raise ValueError(f'checkout escapes output directory: {directory}')
    if (output / 'backends.json').exists():
        raise ValueError(f'registry already exists: {output / "backends.json"}')
    if output.exists() and any(output.iterdir()):
        raise ValueError(f'output directory is not empty: {output}')
    shared = shared_dependencies()
    requirements = {name: dependency_requires(backends[name], output / backends[name]['checkout'], shared)
                    for name in provers}
    if build:
        for name, dependency in shared.items():
            if not Path(dependency['project']).is_dir():
                raise ValueError(f'shared dependency {name} is missing; run lake update in {PROJECT} first')
    # Keep Git entries in the manifests: Mathlib's cache rejects path overrides
    # for its dependencies, even if they contain the very same Git revision.
    registry_shared = {
        name: {**dependency, 'project': os.path.relpath(dependency['project'], output)}
        for name, dependency in shared.items()}
    output.mkdir(parents=True, exist_ok=True)
    definitions = output / 'definitions'
    definitions.mkdir(exist_ok=True)
    snapshot = ET.Element('prover-environment', version='1')
    registry = {'version': 1, 'backends': {}}
    for prover in provers:
        spec = backends[prover]
        directory = output / spec['checkout']
        (directory / 'Benchmark').mkdir(parents=True)
        packages = directory / '.lake/packages'
        packages.mkdir(parents=True)
        for name, dependency in shared.items():
            (packages / name).symlink_to(os.path.relpath(dependency['project'], packages),
                                        target_is_directory=True)
        shared_requires = ''.join(
            (f'require {name} from git {json.dumps(dependency["url"])} @ {json.dumps(dependency["revision"])}\n'
             if dependency['url'] and dependency['revision'] else
             f'require {name} from {json.dumps(os.path.relpath(dependency["project"], directory), ensure_ascii=False)}\n')
            for name, dependency in shared.items())
        (directory / 'lean-toolchain').write_text((PROJECT / 'lean-toolchain').read_text())
        (directory / 'lakefile.lean').write_text(
            'import Lake\nopen Lake DSL\npackage benchmark_prover\n'
            f'require vampire_hammer_benchmark from {json.dumps(os.path.relpath(PROJECT, directory))}\n'
            + shared_requires
            + requirements[prover] + '\n@[default_target]\nlean_lib Benchmark.Prover where\n'
            '  roots := #[`Benchmark.Prover]\n'
            '  precompileModules := true\n'
            '  defaultFacets := #[LeanLib.sharedFacet]\n')
        (directory / 'Benchmark/Prover.lean').write_text(
            # A minimal Mathlib import makes its shared library a native link
            # dependency. Lake then loads its complete dependency graph in
            # order, without importing the corpus into the replay baseline.
            'import Mathlib.Init\nimport VampireHammerBenchmark.Comparison\n' + spec['imports'] +
            '\nnamespace VampireHammer.Benchmark\nopen Lean Elab Term\n\n' +
            spec['declarations'] + '\n' +
            f'initialize registerBenchmarkTactic {json.dumps(prover)} ' + spec['wrapper'] +
            '\nend VampireHammer.Benchmark\n')
        smoke = (PROJECT / 'Tests/Frontend/ComparisonBenchmarkTests.lean').read_text()
        smoke = smoke.replace('import VampireHammerBenchmark.Comparison', 'import Benchmark.Prover')
        smoke = smoke.replace('for prover in #["simp_all", "grind"]', f'for prover in #[{json.dumps(prover)}]')
        smoke = smoke.replace('"Tests/Frontend/ComparisonBenchmarkFixture.lean"',
                              json.dumps(os.path.relpath(PROJECT / 'Tests/Frontend/ComparisonBenchmarkFixture.lean', directory)))
        smoke = smoke.replace('    let options := options.setBool',
                              '    let options := options.set `benchmark.adapterModule "Benchmark.Prover"\n'
                              '    let options := options.setBool')
        (directory / 'Smoke.lean').write_text(smoke)
        snapshot_file = definitions / f'{prover}.xml'
        shutil.copy2(spec['definition'], snapshot_file)
        if any('path' in dep for dep in spec['dependencies']):
            saved = ET.parse(snapshot_file)
            for element, dependency in zip(saved.getroot().findall('dependency'), spec['dependencies']):
                if 'path' in dependency:
                    element.set('path', os.path.relpath(dependency['path'], definitions))
            saved.write(snapshot_file, encoding='utf-8', xml_declaration=True)
        ET.SubElement(snapshot, 'prover', file=f'definitions/{prover}.xml')
        registry['backends'][prover] = {
            'project': spec['checkout'], 'adapter_module': 'Benchmark.Prover',
            'native_library': 'Benchmark.Prover',
            'shared_dependencies': registry_shared,
            'benchmark_imports': 'import Mathlib\nimport Benchmark.Prover\n',
            'artifacts': spec.get('artifacts'),
            'source_dependencies': spec['dependencies'],
            'definition_sha256': hashlib.sha256(spec['definition'].read_bytes()).hexdigest(),
            'definition_snapshot_sha256': hashlib.sha256(snapshot_file.read_bytes()).hexdigest(),
        }
    ET.indent(snapshot)
    ET.ElementTree(snapshot).write(output / 'environment.xml', encoding='utf-8', xml_declaration=True)
    (output / 'backends.json').write_text(json.dumps(registry, indent=2) + '\n')
    if build:
        for prover in provers:
            directory = output / backends[prover]['checkout']
            for command in (['lake', 'update'], ['lake', 'exe', 'cache', 'get'],
                            ['lake', 'build'], ['lake', 'lean', 'Smoke.lean']):
                environment = {key: value for key, value in os.environ.items()
                               if key not in {'LEAN_PATH', 'LEAN_SRC_PATH', 'LEAN_SYSROOT'}}
                subprocess.run(command, cwd=directory, env=environment, check=True)
    return output / 'backends.json'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--definition', type=Path, default=DEFAULT_DEFINITION,
                        help='environment index or single prover XML (default: configurations/provers.xml)')
    parser.add_argument('--prover', action='append',
                        help='repeat to select prover names from the XML; default: all defined provers')
    parser.add_argument('--build', action='store_true', help='update, fetch Mathlib caches, and build every selected project')
    args = parser.parse_args()
    try:
        registry = prepare(args.output, args.prover, args.build, args.definition)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.error(str(error))
    print(f'Prepared isolated prover projects. Registry: {registry}')
    if not args.build:
        print('In each project run: lake update && lake exe cache get && lake build && lake lean Smoke.lean')


if __name__ == '__main__':
    main()
