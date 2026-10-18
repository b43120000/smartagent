"""Run the existing T15 harness against the C release with isolated evidence."""
import argparse
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SOURCE = (ROOT / 'source').resolve()
sys.path.insert(0, str(SOURCE))

import agent_core
import WebAgent
import RemoteAgent

for package in (agent_core, WebAgent, RemoteAgent):
    origin = Path(package.__file__).resolve()
    if not origin.is_relative_to(SOURCE):
        raise RuntimeError(f'WRONG_SMARTAGENT_VERSION: {package.__name__}: {origin}')

parser = argparse.ArgumentParser()
parser.add_argument('--phase', choices=('offline', 'INDEX_ONLY', 'FULL_BUNDLE', 'DELTA', 'recheck_FULL_BUNDLE'), required=True)
parser.add_argument('--run-id', default='C_RELEASE_T15_20261017_V1')
args = parser.parse_args()
if not args.run_id.replace('_', '').isalnum():
    parser.error('run-id must contain only letters, digits and underscores')

script = ROOT / 'localdata' / 'runtime_it' / 'matrix38' / 't15_verify_only.py'
if not script.is_file():
    raise FileNotFoundError(script)
namespace = runpy.run_path(str(script))
main = namespace['main']
state = ROOT / 'localdata' / 'runtime_it' / 'matrix38' / f't15_{args.run_id}.json'
main.__globals__['STATE'] = state
sys.argv = [str(script), '--phase', args.phase]
print('T15_C_SOURCE_VERIFIED', str(SOURCE), flush=True)
print('T15_C_EVIDENCE', str(state), flush=True)
main()
