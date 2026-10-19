"""Build the clean embedded-Python runtime; run only in a disposable build directory."""
import argparse, hashlib, json, shutil, subprocess, sys, urllib.request, zipfile
from pathlib import Path
URL='https://www.python.org/ftp/python/3.13.16/python-3.13.16-embed-amd64.zip'
SHA='97dae5274cc54867065e8d5a3226e48c35017ed332a0fdb0e27d5b5821961297'
MAX=94*1024*1024
def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def main():
 p=argparse.ArgumentParser();p.add_argument('--root',required=True);p.add_argument('--build-root',required=True);a=p.parse_args();root=Path(a.root).resolve();build=Path(a.build_root).resolve();expected=root/'localdata'/'portable_build'
 if build!=expected or root in build.parents is False: raise SystemExit('build-root must be <release>\\localdata\\portable_build')
 stage=build/'stage';wheel=build/'wheels';shutil.rmtree(build,ignore_errors=True);stage.mkdir(parents=True);wheel.mkdir()
 embed=build/'python.zip';urllib.request.urlretrieve(URL,embed)
 if digest(embed)!=SHA:raise SystemExit('official embedded Python SHA256 mismatch')
 with zipfile.ZipFile(embed) as z:z.extractall(stage/'.venv'/'Scripts')
 exe=stage/'.venv'/'Scripts'/'python.exe'; subprocess.check_call([str(exe),'-I','-m','ensurepip']) if False else None
 req=[root/'install_smart_agent'/'requirements.txt',root/'install_smart_agent'/'requirements-webdirect.txt']
 subprocess.check_call([sys.executable,'-m','pip','download','--only-binary=:all:','--platform','win_amd64','--python-version','3.13','--implementation','cp','--abi','cp313','--dest',str(wheel),*sum((['-r',str(x)] for x in req),[])])
 site=stage/'.venv'/'Lib'/'site-packages';site.mkdir(parents=True)
 for whl in wheel.glob('*.whl'):
  with zipfile.ZipFile(whl) as z:z.extractall(site)
 pth=stage/'.venv'/'Scripts'/'python313._pth';pth.write_text('python313.zip\n.\n../Lib/site-packages\n../../source\nimport site\n',encoding='utf-8')
 subprocess.check_call([str(exe),'-I','-B','-c',"import PIL._imaging,greenlet,playwright,qrcode,googleapiclient,google_auth_oauthlib; from pathlib import Path; from playwright._impl._driver import compute_driver_executable; assert Path(compute_driver_executable()[0]).exists()"])
 archive=build/'portable-runtime.zip'
 with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
  for f in stage.rglob('*'):
   if f.is_file():z.write(f,f.relative_to(stage))
 out=root/'.venv'/'portable-runtime';shutil.rmtree(out,ignore_errors=True);out.mkdir(parents=True)
 data=archive.read_bytes();parts=[]
 for i in range(0,len(data),MAX):
  part=out/f'runtime.zip.part{i//MAX:03d}';part.write_bytes(data[i:i+MAX]);parts.append({'name':part.name,'sha256':digest(part),'bytes':part.stat().st_size})
 lock=[{'name':x.name,'sha256':digest(x),'bytes':x.stat().st_size} for x in sorted(wheel.glob('*.whl'))]
 if not lock: raise SystemExit('wheel resolution produced no locked artifacts')
 (out/'requirements-lock.json').write_text(json.dumps(lock,indent=2),encoding='utf-8')
 (out/'LICENSES.json').write_text(json.dumps({'python':{'license':'PSF','url':'https://docs.python.org/3/license.html'},'wheels':[x['name'] for x in lock],'notice':'Wheel METADATA and license files are retained in extracted *.dist-info directories.'},indent=2),encoding='utf-8')
 (out/'README.md').write_text('Immutable offline Windows embedded-Python runtime payload. Run ensure_portable_runtime.bat to validate and extract it.\n',encoding='utf-8')
 (out/'manifest.json').write_text(json.dumps({'schema':'SMARTAGENT_PORTABLE_RUNTIME_V1','python':'3.13.16','architecture':'x64','python_url':URL,'python_sha256':SHA,'archive_sha256':digest(archive),'parts':parts,'requirements_lock':'requirements-lock.json'},indent=2),encoding='utf-8')
if __name__=='__main__':main()
