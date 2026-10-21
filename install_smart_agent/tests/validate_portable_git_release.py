"""Fresh-directory offline acceptance; run with a host Python only as test driver."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import zipfile


def run(root, output):
    root = Path(root).resolve()
    output = Path(output).resolve()
    if output.exists():
        raise RuntimeError('TEST_OUTPUT_MUST_BE_FRESH')
    output.mkdir(parents=True)
    fresh = output / 'fresh package with spaces'
    fresh.mkdir()
    for folder in ['source', 'install_smart_agent', 'adapterUI', 'config']:
        shutil.copytree(root / folder, fresh / folder, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', 'debug_config.json', 'dom_config.history'))
    for name in ['ACLstatus.bat', 'Edit_workspace.bat', 'force_stop_all_agents.bat', 'launch_remote_agent.bat', 'launch_webcopilot.bat']:
        shutil.copyfile(root / name, fresh / name)
    for folder in ['portable-runtime', 'portable-git']:
        shutil.copytree(root / '.venv' / folder, fresh / '.venv' / folder)
    env = dict(os.environ, PATH=os.environ['SystemRoot']+'\\System32', PYTHONIOENCODING='utf-8',
        TEMP=str(output), TMP=str(output), SMARTAGENT_TEST_NO_PAUSE='1')
    env.pop('PYTHONPATH', None)
    checks = []

    def command(name, args, *, cwd=fresh, expected=0):
        result = subprocess.run([str(x) for x in args], cwd=cwd, env=env, capture_output=True,
            text=True, encoding='utf-8', errors='replace', timeout=180)
        checks.append(dict(name=name, passed=result.returncode == expected, exit=result.returncode,
            output=(result.stdout+result.stderr)[-6000:]))
        (output / 'report.json').write_text(json.dumps(checks, indent=2), encoding='utf-8')
        if result.returncode != expected:
            raise RuntimeError(name+': '+checks[-1]['output'])
        return result.stdout.strip()

    ps = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    bootstrap = [ps, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', fresh/'install_smart_agent/ensure_environment.ps1', '-ProjectRoot', fresh]
    command('fresh_extract_without_host_python_or_git', bootstrap)
    command('second_launch_idempotent', bootstrap)
    python = fresh / '.venv/Scripts/python.exe'
    git = fresh / '.venv/git/cmd/git.exe'
    env['PATH'] = str(python.parent)+';'+str(git.parent)+';'+env['PATH']
    command('bundled_pip', [python, '-I', '-m', 'pip', '--version'])
    command('imports_and_protocol', [python, '-I', '-B', '-c',
        'import PIL,playwright,greenlet; from agent_core.protocol_manifest import require_protocol_manifest; print(require_protocol_manifest(r"'+str(fresh)+'"))'])
    for launcher in fresh.glob('*.bat'):
        command(launcher.name, [os.environ['SystemRoot']+'\\System32\\cmd.exe', '/d', '/c', 'call', str(launcher), '--launcher-self-test'])
    # A missing library is installed locally and verified using the embedded pip.
    sdk = output / 'probe_sdk-1.0-py3-none-any.whl'
    with zipfile.ZipFile(sdk, 'w') as wheel:
        wheel.writestr('probe_sdk/__init__.py', 'VALUE = "installed locally"\n')
        wheel.writestr('probe_sdk-1.0.dist-info/METADATA', 'Metadata-Version: 2.1\nName: probe-sdk\nVersion: 1.0\n')
        wheel.writestr('probe_sdk-1.0.dist-info/WHEEL', 'Wheel-Version: 1.0\nGenerator: SmartAgent IT\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
        wheel.writestr('probe_sdk-1.0.dist-info/RECORD', '')
    target = output / 'project dependencies'
    command('missing_dependency_detected', [python, '-I', '-c', 'import probe_sdk'], expected=1)
    command('install_local_dependency', [python, '-I', '-m', 'pip', 'install', '--no-index', '--no-deps', '--target', target, sdk])
    command('verify_and_retry_dependency', [python, '-I', '-c', 'import sys;sys.path.insert(0,r"'+str(target)+'");import probe_sdk;assert probe_sdk.VALUE=="installed locally"'])
    source = output / 'execution repo'; source.mkdir()
    destination = output / 'publication repo'
    command('git_init', [git, 'init', source])
    for key, value in [('user.name','SmartAgent IT'), ('user.email','smartagent-it@example.invalid')]:
        command('source_identity', [git, '-C', source, 'config', key, value])
    (source/'base.txt').write_text('base', encoding='utf-8')
    command('git_add_base', [git, '-C', source, 'add', 'base.txt'])
    command('git_commit_base', [git, '-C', source, 'commit', '-m', 'base'])
    command('git_clone', [git, 'clone', source, destination])
    for key, value in [('user.name','SmartAgent IT'), ('user.email','smartagent-it@example.invalid')]:
        command('destination_identity', [git, '-C', destination, 'config', key, value])
    (source/'feature.txt').write_text('portable transfer', encoding='utf-8')
    command('git_add_feature', [git, '-C', source, 'add', 'feature.txt'])
    command('git_commit_feature', [git, '-C', source, 'commit', '-m', 'feature'])
    sha = command('git_feature_sha', [git, '-C', source, 'rev-parse', 'HEAD'])
    # The production transfer resolves Git relative to the execution checkout.
    (source/'.venv').mkdir()
    shutil.copytree(fresh/'.venv/git', source/'.venv/git')
    (source/'.git/info/exclude').write_text('.venv/\n', encoding='utf-8')
    transfer = [ps, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', fresh/'install_smart_agent/git_release.ps1', '-SourceRoot', source, '-TargetRoot', destination, '-Commit', sha]
    command('bundled_git_fetch_cherry_pick', transfer)
    assert (destination/'feature.txt').read_text() == 'portable transfer'
    command('transfer_replay_idempotent', transfer)
    (destination/'dirty.txt').write_text('keep', encoding='utf-8')
    command('dirty_destination_rejected', transfer, expected=1)
    print(json.dumps(dict(passed=True, checks=len(checks), report=str(output/'report.json'))))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    run(args.root, args.output)
