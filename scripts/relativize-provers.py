#!/usr/bin/env python3
"""Make existing compiled prover projects relocatable without rebuilding them."""
import argparse
import json
import os
from pathlib import Path


def relativize(root):
    root = Path(root).resolve()
    provers = root / 'artifacts/provers'
    if not (provers / 'backends.json').is_file():
        raise ValueError(f'missing registry: {provers / "backends.json"}')

    def relative_json(value, base):
        if isinstance(value, dict):
            return {key: relative_json(item, base) for key, item in value.items()}
        if isinstance(value, list):
            return [relative_json(item, base) for item in value]
        if isinstance(value, str) and (value == str(root) or value.startswith(str(root) + '/')):
            return os.path.relpath(value, base)
        return value

    # Do not follow shared dependency symlinks or modify compiled files/traces.
    for directory, directories, files in os.walk(provers, followlinks=False):
        base = Path(directory)
        for name in directories + files:
            link = base / name
            if link.is_symlink():
                target = link.resolve()
                if Path(os.readlink(link)).is_absolute() and target.is_relative_to(root):
                    link.unlink()
                    link.symlink_to(os.path.relpath(target, base), target_is_directory=target.is_dir())
        for name in files:
            path = base / name
            if path.is_symlink():
                continue
            if name in ('backends.json', 'lake-manifest.json'):
                before = json.loads(path.read_text())
                after = relative_json(before, base)
                if after != before:
                    path.write_text(json.dumps(after, indent=2) + '\n')
            elif base.parent == provers and name in ('lakefile.lean', 'Smoke.lean'):
                before = path.read_text()
                after = before.replace(str(root), os.path.relpath(root, base))
                if after != before:
                    path.write_text(after)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        relativize(args.project)
    except ValueError as error:
        parser.error(str(error))
    print('Prover paths are relative; compiled artifacts were preserved.')


if __name__ == '__main__':
    main()
