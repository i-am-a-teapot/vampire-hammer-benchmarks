#!/usr/bin/env python3
"""Run serial, bounded portfolio probes; reject oversized/include-based inputs."""
import importlib.util
import argparse
import json
import re
import subprocess
import time
from pathlib import Path


spec = importlib.util.spec_from_file_location('benchmark_mathlib', Path(__file__).with_name('benchmark-mathlib.py'))
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('inputs', nargs='+', type=Path)
    parser.add_argument('--vampire',
                        default=str(benchmark.vampire_executable()))
    parser.add_argument('--output', type=Path, default=Path('artifacts/portfolio-probe'))
    parser.add_argument('--max-bytes', type=int, default=4096)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    rows = []
    for index, path in enumerate(args.inputs):
        size = path.stat().st_size
        row = dict(input=str(path), bytes=size)
        if size > args.max_bytes:
            row['skipped'] = 'input exceeds size limit'
        else:
            source = path.read_text()
            # Include directives may hide arbitrarily large axiom files.
            if re.search(r'^\s*include\s*\(', source, re.M):
                row['skipped'] = 'includes need separate size checks'
            else:
                command = [args.vampire, '--mode', 'portfolio', '--cores', '1',
                           '--shuffle_input', 'off', '--shuffle_on_schedule_repeats', 'off',
                           '--forced_options', 'proof=tptp_ac:replay=on:skolemization=syntactic:shuffle_input=off:proof_extra=replay:memory_limit=512',
                           '-p', 'tptp_ac', '--replay', 'on', '--skolemization', 'syntactic',
                           '--proof_extra', 'replay', '--time_limit', '3', '--memory_limit', '512']
                start = time.monotonic()
                result = subprocess.run(command, input=source, text=True, capture_output=True, timeout=15)
                row.update(exit_code=result.returncode, wall_seconds=round(time.monotonic()-start, 4),
                           status=re.findall(r'SZS status (\w+)', result.stdout),
                           rules=sorted(set(re.findall(r'inference\((\w+)', result.stdout))))
                stem = args.output / f'{index:02d}-{path.stem}'
                Path(str(stem)+'.stdout').write_text(result.stdout)
                Path(str(stem)+'.stderr').write_text(result.stderr)
        rows.append(row)
        print(json.dumps(row), flush=True)
    (args.output / 'metrics.json').write_text(json.dumps(rows, indent=2)+'\n')


if __name__ == '__main__':
    main()
