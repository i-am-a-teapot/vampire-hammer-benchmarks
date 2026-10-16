#!/usr/bin/env python3
"""Forward regression invocations to the dependency's certificate binary."""
import importlib.util
import os
from pathlib import Path
import sys

spec = importlib.util.spec_from_file_location('benchmark_mathlib', Path(__file__).with_name('benchmark-mathlib.py'))
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)
executable = os.environ.get('VAMPIRE_BIN') or str(benchmark.vampire_executable())
os.execv(executable, [executable, *sys.argv[1:]])
