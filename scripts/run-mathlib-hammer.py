#!/usr/bin/env python3
"""Run the real Lean hammer suite sequentially, retaining bounded-run artifacts."""

import argparse
import datetime
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vampire", default="vampire", help="replay-enabled Vampire binary")
    parser.add_argument("--memory-mib", type=int, default=512)
    parser.add_argument("--timeout", type=float, default=180, help="whole-suite wall limit")
    parser.add_argument("--output", type=Path, help="new artifact directory")
    args = parser.parse_args()
    if args.memory_mib <= 0 or args.timeout <= 0:
        parser.error("memory and timeout must be positive")
    binary = shutil.which(args.vampire)
    if binary is None:
        parser.error(f"Vampire executable not found: {args.vampire}")
    binary = str(Path(binary).resolve())
    project = Path(__file__).resolve().parents[1]
    output = args.output or Path("artifacts") / datetime.datetime.now().strftime(
        "mathlib-hammer-%Y%m%d-%H%M%S"
    )
    if not output.is_absolute():
        output = project / output
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    source = (project / "Tests/Frontend/MathlibHammerTests.lean").read_text()
    # Retain the exact source, with distinct input paths for this run.
    prefix = (output / "problems").as_posix() + "/"
    source = source.replace('"artifacts/mathlib-hammer/', json.dumps(prefix)[:-1])
    snapshot = output / "MathlibHammerTests.lean"
    snapshot.write_text(source)
    started = time.monotonic()
    timed_out = False
    with tempfile.TemporaryDirectory(prefix="mathlib-hammer-") as temp:
        wrapper = Path(temp) / "vampire.py"
        wrapper.write_text(
            "#!/usr/bin/env python3\nimport os, sys\nargs = sys.argv[1:]\n"
            "if '--forced_options' in args:\n"
            "    i = args.index('--forced_options') + 1\n"
            f"    args[i] += ':memory_limit={args.memory_mib}'\n"
            f"os.execv({binary!r}, ['vampire', *args, '--memory_limit', {str(args.memory_mib)!r}])\n"
        )
        wrapper.chmod(0o700)
        # A source-level executable option overrides -D; keep every invocation
        # routed through the bounded wrapper even when the suite sets a binary.
        snapshot.write_text(re.sub(
            r'(set_option vampire\.executable )"[^"\n]*"',
            lambda match: match[1] + json.dumps(str(wrapper)), source,
        ))
        # `lake lean` loads the precompiled hammer and lean-auto libraries;
        # `lake env lean` would interpret their code (see benchmark-mathlib.py).
        command = [
            "lake", "-R", "-KbuildVampire=false", "lean", str(snapshot), "--", "-j", "1",
            f"-Dvampire.executable={wrapper}",
        ]
        with (output / "suite.log").open("w") as log:
            process = subprocess.Popen(
                command, cwd=project, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                returncode = process.wait(timeout=args.timeout)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                timed_out = True
                os.killpg(process.pid, signal.SIGKILL)
                returncode = process.wait()
    rows = []
    for problem in sorted((output / "problems").glob("*.p")):
        stdout = Path(str(problem) + ".stdout")
        text = stdout.read_text() if stdout.exists() else ""
        search = re.findall(r"% Time elapsed: ([0-9.]+) s", text)
        memory = re.findall(r"% Peak memory usage: ([0-9]+) MB", text)
        exitfile = Path(str(problem) + ".exitcode")
        rows.append({
            "name": problem.stem,
            "input_bytes": problem.stat().st_size,
            "vampire_exit": int(exitfile.read_text()) if exitfile.exists() else None,
            "vampire_reported_seconds": float(search[-1]) if search else None,
            "vampire_peak_memory_mb": int(memory[-1]) if memory else None,
        })
    summary = {
        "passed": returncode == 0 and not timed_out,
        "returncode": returncode,
        "timed_out": timed_out,
        "suite_wall_seconds": round(time.monotonic() - started, 3),
        "vampire": binary,
        "memory_limit_mib": args.memory_mib,
        "problems": rows,
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(f"{'PASS' if summary['passed'] else 'FAIL'}: {len(rows)} exported problems; "
          f"{summary['suite_wall_seconds']} s; artifacts: {output}")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
