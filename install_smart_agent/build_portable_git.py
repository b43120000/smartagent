"""Reproduce the pinned official MinGit payload (no host Git installation needed)."""
import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import urllib.request
import zipfile

URL = 'https://github.com/git-for-windows/git/releases/download/v2.56.0.windows.2/MinGit-2.56.0.2-64-bit.zip'
SHA = 'da35e72aa21c005a5a0d298cfbae110bc1609a815730ea0dde84b01a1b3cd3be'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    root = Path(parser.parse_args().root).resolve()
    payload = root / '.venv' / 'portable-git'
    payload.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=payload) as temporary:
        archive = Path(temporary) / 'git.zip'
        urllib.request.urlretrieve(URL, archive)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != SHA:
            raise SystemExit('PORTABLE_GIT_OFFICIAL_HASH_MISMATCH')
        with zipfile.ZipFile(archive) as package:
            binary_sha = hashlib.sha256(package.read('cmd/git.exe')).hexdigest()
        manifest = dict(schema='SMARTAGENT_PORTABLE_GIT_V1', version='2.56.0.windows.2',
            architecture='x64', url=URL, archive_sha256=SHA, bytes=archive.stat().st_size,
            binary_sha256=binary_sha, license='GPL-2.0; distribution retains upstream notices',
            source='https://github.com/git-for-windows/git/tree/v2.56.0.windows.2')
        (payload / 'manifest.json.tmp').write_text(json.dumps(manifest, indent=2)+'\n', encoding='utf-8')
        archive.replace(payload / 'git.zip')
        (payload / 'manifest.json.tmp').replace(payload / 'manifest.json')


if __name__ == '__main__':
    main()
