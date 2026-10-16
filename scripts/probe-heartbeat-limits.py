#!/usr/bin/env python3
import argparse, os, subprocess, pathlib, json, time, concurrent.futures
import importlib.util
spec = importlib.util.spec_from_file_location("suite", pathlib.Path(__file__).resolve().parents[1] / "slurmrunner/make-suite.py")
suite = importlib.util.module_from_spec(spec)
spec.loader.exec_module(suite)
parser = argparse.ArgumentParser(description="Probe heartbeat limits using native Lean declaration replay.")
parser.add_argument('--project', type=pathlib.Path, default=pathlib.Path(__file__).resolve().parents[1])
parser.add_argument('--extra-project', type=pathlib.Path, help="A built checkout supplying mathlib and its dependencies")
parser.add_argument('--output', type=pathlib.Path, default=pathlib.Path('artifacts/heartbeat-probe'))
parser.add_argument('--heartbeats', type=int, action='append')
parser.add_argument('--synthesis-heartbeats', type=int)
parser.add_argument('--without-data-witnesses', action='store_true')
parser.add_argument('--case', action='append', help="THEOREM@MODULE (repeatable)")
parser.add_argument('--timeout', type=float, default=90)
parser.add_argument('--workers', type=int, default=1)
options = parser.parse_args()
root = options.project.resolve()
work = root
out = options.output.resolve()
if options.workers < 1 or options.timeout <= 0:
 parser.error('--workers and --timeout must be positive')
if any(hb < 0 for hb in (options.heartbeats or [])):
 parser.error('--heartbeats must be nonnegative')
out.mkdir(parents=True, exist_ok=True)
env=dict(os.environ)
for key in ['LEAN_PATH','LD_LIBRARY_PATH']:
 env[key]=subprocess.check_output(['lake','env','printenv',key],cwd=root,text=True).strip()
if options.extra_project:
 env['LEAN_PATH']+=':'+':'.join(str(p/'.lake/build/lib/lean') for p in (options.extra_project/'.lake/packages').iterdir())
env['LEAN_SRC_PATH']=subprocess.check_output(['lake','env','printenv','LEAN_SRC_PATH'],cwd=root,text=True).strip()
if options.extra_project:
 env['LEAN_SRC_PATH']+=':'+':'.join(str(p) for p in (options.extra_project/'.lake/packages').iterdir())
suite.PROJECT = root
suite.benchmark.PROJECT = root
prefix = subprocess.check_output(['lake', 'env', 'lean', '--print-prefix'], cwd=root, text=True).strip()
args = [str(pathlib.Path(prefix) / 'bin/lean'), '-j', '1',
        *suite.native_lean_args(imports='import VampireHammerBenchmark.Mathlib\n')]
cases = [tuple(case.split('@', 1)) for case in options.case] if options.case else [
 ('AlgEquiv.arrowCongr_comp', 'Mathlib.Algebra.Algebra.Equiv'),
 ('AffineAddMonoid.embedding_injective', 'Mathlib.Algebra.AffineMonoid.Embedding')]
if any(len(case) != 2 for case in cases):
 parser.error('--case must be THEOREM@MODULE')
configs = [(str(hb), hb, options.synthesis_heartbeats) for hb in (options.heartbeats or [65536, 262144])]

def run(case,config):
 name,module=case;tag,hb,synth=config;stem=name.replace('.','_')+'-'+tag;source=out/(stem+'.lean');events=out/(stem+'.jsonl');log=out/(stem+'.log')
 events.write_text('')
 opts=f'set_option maxHeartbeats {hb}\n'+(f'set_option synthInstance.maxHeartbeats {synth}\n' if synth is not None else '')
 opts += 'set_option vampire.dataWitnesses false\n' if options.without_data_witnesses else ''
 source.write_text(f'import {module}\nimport VampireHammerBenchmark.Mathlib\n'+opts+f'''set_option vampire.executable {json.dumps(str(suite.benchmark.vampire_executable(root)), ensure_ascii=False)}
set_option vampire.timeLimit 10
set_option vampire.higherOrder false
set_option stderrAsMessages false
set_option vampire.suggestedLemmas 0
set_option vampire.siblingLemmas false
set_option vampire.omitUnsupportedLocalHypotheses true
set_option vampire.alignNestedDefinitions true
hammer_mathlib_try_with_premises {json.dumps(name, ensure_ascii=False)} recording {json.dumps(str(events), ensure_ascii=False)}
''')
 start=time.monotonic()
 with log.open('w') as f:
  try:r=subprocess.run([*args,str(source)],cwd=work,env=env,stdout=f,stderr=subprocess.STDOUT,timeout=options.timeout);status='exit '+str(r.returncode)
  except subprocess.TimeoutExpired:status='process timeout'
 rows=[json.loads(l) for l in events.read_text().splitlines() if l.endswith('}')] if events.exists() else []
 result={'name':name,'config':tag,'heartbeat_limit':hb,'synthesis_limit':synth,'wall_seconds':round(time.monotonic()-start,3),'process':status,'result':next((r for r in reversed(rows) if r.get('event')=='finished'),None)}
 print(json.dumps(result),flush=True);return result
with concurrent.futures.ThreadPoolExecutor(max_workers=options.workers) as pool:
 futures=[pool.submit(run,c,k) for c in cases for k in configs];rs=[f.result() for f in futures]
(out/'summary.json').write_text(json.dumps(rs,indent=2))
