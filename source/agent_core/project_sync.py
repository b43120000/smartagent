#!/usr/bin/env python3
from __future__ import annotations
import hashlib,json,os,subprocess,time
from collections import Counter
from pathlib import Path
from .project_artifact_policy import classify_project_file
from .path_security import is_reparse_path

SNAPSHOT_SCHEMA_VERSION=5
IGNORED_DIRS={'.git','.agents','.venv','venv','env','build','out','dist','__pycache__','.gradle','.idea','.vs','node_modules'}
LOCALDATA_SYNC_EXCLUDED_DIRS={'runtime','cache','logs','temp','secure','bindings','persistent','metadata'}
BUILD_NAMES={'CMakeLists.txt','build.gradle','build.gradle.kts','settings.gradle','settings.gradle.kts','Android.mk','Android.bp'}
LANGUAGE_BY_EXT={'.c':'c','.h':'c-cpp-header','.cc':'cpp','.cpp':'cpp','.cxx':'cpp','.hpp':'cpp-header','.java':'java','.kt':'kotlin','.kts':'kotlin','.py':'python','.js':'javascript','.ts':'typescript','.rs':'rust','.go':'go','.cmake':'cmake','.gradle':'gradle','.xml':'xml','.json':'json','.md':'markdown'}

def is_generated_runtime_path(path:str|Path)->bool:
 """Return True for agent-owned state that must never enter source sync.

 The WebAgent state directory contains append-only logs and conversation locks.
 Those files are mutable runtime evidence, not project ground truth.  Matching
 consecutive path components also covers packaged copies such as
 ``app/source/WebAgent/state`` without excluding a normal project ``state`` folder.
 """
 parts=[part.casefold() for part in Path(path).parts]
 if any(parts[index]=='localdata' and parts[index+1] in LOCALDATA_SYNC_EXCLUDED_DIRS for index in range(len(parts)-1)):
  return True
 return any(
  parts[index] in {'webagent','remoteagent'} and parts[index+1]=='state'
  for index in range(len(parts)-1)
 )

def is_ignored_project_path(path:str|Path)->bool:
 """Return True for paths excluded from both inventory and Git-state identity."""
 parts=[part.casefold() for part in Path(path).parts]
 if any(part in IGNORED_DIRS for part in parts): return True
 if any(part.startswith('.update_') for part in parts): return True
 return is_generated_runtime_path(path)

def _stable_git_status(root:Path)->list[str]:
 """Keep snapshot identity stable when Runtime writes its own ignored state."""
 stable=[]
 for line in _git(root,'status','--porcelain','--untracked-files=all').splitlines():
  raw=line[3:].strip() if len(line)>=4 else ''
  # Rename/copy porcelain rows use ``old -> new``; either ignored endpoint
  # is Runtime/generated state and must not invalidate a source snapshot.
  candidates=[item.strip().strip('"') for item in raw.split(' -> ') if item.strip()]
  if candidates and any(is_ignored_project_path(item) for item in candidates): continue
  stable.append(line)
 return sorted(stable)

def _sha256_file(path:Path)->str:
 h=hashlib.sha256()
 with path.open('rb') as f:
  for chunk in iter(lambda:f.read(1024*1024),b''): h.update(chunk)
 return h.hexdigest()

def _git(root:Path,*args:str)->str:
 try:
  r=subprocess.run(['git','-C',str(root),*args],capture_output=True,text=True,timeout=10,encoding='utf-8',errors='replace')
  return (r.stdout or '').strip() if r.returncode==0 else ''
 except Exception:return ''

def _git_index_blobs(root:Path)->dict[str,str]:
 """Return tracked path -> Git blob identity, or an empty map outside Git."""
 rows={}
 for line in _git(root,'ls-files','-s').splitlines():
  if '\t' not in line: continue
  meta,path=line.split('\t',1);parts=meta.split()
  if len(parts)>=2 and path and not is_ignored_project_path(path): rows[path.replace('\\','/')]=parts[1]
 return rows

def _git_dirty_paths(root:Path)->set[str]:
 paths=set()
 for args in (
  ('diff','--name-only'),('diff','--cached','--name-only'),
  ('ls-files','--others','--exclude-standard'),
 ):
  for line in _git(root,*args).splitlines():
   rel=line.strip().replace('\\','/')
   if rel and not is_ignored_project_path(rel): paths.add(rel)
 return paths

def _load_incremental_baseline(root:Path)->dict[str,dict]:
 """Load the last published snapshot without making it a correctness gate."""
 index=root/'.agents'/'project_context'/'current.json'
 try:
  meta=json.loads(index.read_text(encoding='utf-8'));sid=str(meta.get('snapshot_id',''))
  snapshot=json.loads((index.parent/'snapshots'/f'{sid}.json').read_text(encoding='utf-8'))
  if Path(str(snapshot.get('workspace_root',''))).resolve()!=root.resolve(): return {}
  return {str(row.get('path','')):row for row in snapshot.get('files',[]) if isinstance(row,dict)}
 except Exception:return {}

def _records(root:Path)->tuple[list[dict],int,Counter,list[str],dict]:
 records=[];dirs=0;exts=Counter();build=[]
 previous=_load_incremental_baseline(root);git_blobs=_git_index_blobs(root);dirty=_git_dirty_paths(root)
 git_mode=bool(git_blobs or _git(root,'rev-parse','--show-toplevel'))
 reused=0;hashed=0
 for current,dirnames,filenames in os.walk(root,followlinks=False):
  base=Path(current);safe_dirs=[]
  for d in sorted(dirnames):
   p=base/d
   rel_dir=p.relative_to(root)
   if is_ignored_project_path(rel_dir) or is_reparse_path(p): continue
   safe_dirs.append(d)
  dirnames[:]=safe_dirs;dirs+=len(dirnames)
  for name in sorted(filenames):
   p=base/name
   if is_reparse_path(p): continue
   rel=p.relative_to(root).as_posix()
   if is_ignored_project_path(rel): continue
   try: stat=p.stat()
   except OSError: continue
   ext=p.suffix.lower() or '[no_extension]';exts[ext]+=1
   if name in BUILD_NAMES or p.suffix.lower() in {'.sln','.vcxproj'}: build.append(rel)
   language='cmake' if name=='CMakeLists.txt' else LANGUAGE_BY_EXT.get(p.suffix.lower(),ext)
   artifact=classify_project_file(p)
   prior=previous.get(rel,{})
   blob=git_blobs.get(rel,'')
   can_reuse_git=bool(blob and rel not in dirty and prior.get('git_blob')==blob and prior.get('sha256'))
   can_reuse_fs=bool(not git_mode and prior.get('size_bytes')==int(stat.st_size) and prior.get('mtime_ns')==int(stat.st_mtime_ns) and prior.get('sha256'))
   try:
    if can_reuse_git or can_reuse_fs:
     digest=str(prior['sha256']);reused+=1
    else:
     digest=_sha256_file(p);hashed+=1
   except OSError as exc: raise ValueError(f'project_file_unreadable:{rel}') from exc
   records.append({'path':rel,'extension':ext,'language':language,'size_bytes':int(stat.st_size),'mtime_ns':int(stat.st_mtime_ns),'sha256':digest,'git_blob':blob,'content_identity':('git:'+blob) if blob and rel not in dirty else ('sha256:'+digest),'identity_source':'GIT_BLOB' if blob and rel not in dirty else 'FILESYSTEM_SHA256','artifact_class':artifact['classification'],'sync_mode':artifact['sync_mode'],'max_single_file_bytes':artifact['max_single_file_bytes'],'oversize_behavior':artifact['oversize_behavior'],'artifact_priority':artifact['priority']})
 records.sort(key=lambda x:x['path'])
 return records,dirs,exts,sorted(build),{'mode':'GIT_FIRST_HYBRID' if git_mode else 'FILESYSTEM_INCREMENTAL','reused_hash_count':reused,'hashed_file_count':hashed,'dirty_path_count':len(dirty)}

def inspect_project_scope(workspace:str|Path)->dict:
 started=time.perf_counter();root=Path(workspace).expanduser().resolve()
 if not root.is_dir(): raise ValueError(f'workspace_not_directory: {root}')
 records,dirs,exts,build,inventory=_records(root)
 git_head=_git(root,'rev-parse','HEAD');git_status_lines=_stable_git_status(root)
 manifest_basis={'schema_version':SNAPSHOT_SCHEMA_VERSION,'files':[{'path':r['path'],'size_bytes':r['size_bytes'],'content_identity':r['content_identity']} for r in records],'git_head':git_head,'git_status':git_status_lines}
 manifest_hash=hashlib.sha256(json.dumps(manifest_basis,sort_keys=True,separators=(',',':')).encode()).hexdigest()
 snapshot_id=hashlib.sha256((f'{SNAPSHOT_SCHEMA_VERSION}\n{manifest_hash}\n{git_head}\n'+'\n'.join(git_status_lines)).encode()).hexdigest()
 return {'schema':'PROJECT_SNAPSHOT_V1','schema_version':SNAPSHOT_SCHEMA_VERSION,'workspace_root':str(root),'snapshot_id':snapshot_id,'manifest_hash':manifest_hash,'git_head':git_head,'git_dirty':bool(git_status_lines),'git_status':git_status_lines[:200],'inventory_mode':inventory['mode'],'reused_hash_count':inventory['reused_hash_count'],'hashed_file_count':inventory['hashed_file_count'],'dirty_path_count':inventory['dirty_path_count'],'file_count':len(records),'directory_count':dirs,'total_bytes':sum(r['size_bytes'] for r in records),'extension_stats':dict(sorted(exts.items())),'build_files':build,'files':records,'elapsed_ms':round((time.perf_counter()-started)*1000,3)}

def inspect_project_working_set(workspace:str|Path,paths:object)->dict:
 """Return a snapshot-bound, exact task working set without source upload."""
 root=Path(workspace).expanduser().resolve()
 if not root.is_dir():raise ValueError(f'workspace_not_directory: {root}')
 if not isinstance(paths,(list,tuple,set)) or not paths: raise ValueError('project_working_set_paths_invalid')
 requested=[]
 for raw in paths:
  text=str(raw or '').strip()
  if not text: continue
  candidate=Path(text).expanduser()
  if candidate.is_absolute():
   try:text=candidate.resolve().relative_to(root).as_posix()
   except ValueError as exc:raise ValueError(f'project_working_set_path_outside_workspace:{text}') from exc
  else:
   parts=Path(text.replace('\\','/')).parts
   if '..' in parts:raise ValueError(f'project_working_set_path_outside_workspace:{text}')
   text=Path(*parts).as_posix().lstrip('/')
  if text and text not in requested:requested.append(text)
 previous=_load_incremental_baseline(root);git_blobs=_git_index_blobs(root);dirty=_git_dirty_paths(root)
 git_head=_git(root,'rev-parse','HEAD');git_mode=bool(git_blobs or _git(root,'rev-parse','--show-toplevel'))
 selected=[];missing=[];reused=0;hashed=0
 for rel in requested:
  p=(root/Path(rel)).resolve()
  try:p.relative_to(root)
  except ValueError as exc:raise ValueError(f'project_working_set_path_outside_workspace:{rel}') from exc
  if not p.is_file() or is_reparse_path(p) or is_ignored_project_path(rel):missing.append(rel);continue
  stat=p.stat();blob=git_blobs.get(rel,'');prior=previous.get(rel,{})
  can_reuse_git=bool(blob and rel not in dirty and prior.get('git_blob')==blob and prior.get('sha256'))
  can_reuse_fs=bool(not git_mode and prior.get('size_bytes')==int(stat.st_size) and prior.get('mtime_ns')==int(stat.st_mtime_ns) and prior.get('sha256'))
  if can_reuse_git or can_reuse_fs:digest=str(prior['sha256']);reused+=1
  else:digest=_sha256_file(p);hashed+=1
  ext=p.suffix.lower() or '[no_extension]';name=p.name
  language='cmake' if name=='CMakeLists.txt' else LANGUAGE_BY_EXT.get(p.suffix.lower(),ext)
  artifact=classify_project_file(p)
  selected.append({'path':rel,'extension':ext,'language':language,'size_bytes':int(stat.st_size),'mtime_ns':int(stat.st_mtime_ns),'sha256':digest,'git_blob':blob,'content_identity':('git:'+blob) if blob and rel not in dirty else ('sha256:'+digest),'identity_source':'GIT_BLOB' if blob and rel not in dirty else 'FILESYSTEM_SHA256','artifact_class':artifact['classification'],'sync_mode':artifact['sync_mode'],'max_single_file_bytes':artifact['max_single_file_bytes'],'oversize_behavior':artifact['oversize_behavior'],'artifact_priority':artifact['priority']})
 basis={'schema_version':SNAPSHOT_SCHEMA_VERSION,'working_set':requested,'files':[{'path':row['path'],'content_identity':row['content_identity']} for row in selected],'git_head':git_head}
 snapshot_id=hashlib.sha256(json.dumps(basis,sort_keys=True,separators=(',',':')).encode()).hexdigest()
 return {'schema':'PROJECT_WORKING_SET_V1','status':'READY' if not missing else 'INCOMPLETE','workspace_root':str(root),'snapshot_id':snapshot_id,'inventory_mode':'GIT_FIRST_HYBRID' if git_mode else 'FILESYSTEM_INCREMENTAL','git_head':git_head,'git_dirty':any(path in dirty for path in requested),'requested_paths':requested,'missing_paths':sorted(missing),'file_count':len(selected),'reused_hash_count':reused,'hashed_file_count':hashed,'files':selected,'attachments_uploaded':0,'scope':'TASK_WORKING_SET'}

def compare_project_snapshot(current:dict,known:dict|None)->dict:
 if known is not None and not isinstance(known,dict): raise TypeError(f'known snapshot must be dict or None; got {type(known).__name__}')
 if not known:return {'freshness':'UNKNOWN','known_snapshot_id':'','current_snapshot_id':current.get('snapshot_id',''),'changed_file_count':current.get('file_count',0),'changed_paths':[r['path'] for r in current.get('files',[])]}
 old={r['path']:r.get('sha256') for r in known.get('files',[])};new={r['path']:r.get('sha256') for r in current.get('files',[])}
 changed=sorted(p for p in set(old)|set(new) if old.get(p)!=new.get(p))
 same_git=(known.get('git_head')==current.get('git_head') and known.get('git_status',[])==current.get('git_status',[]))
 return {'freshness':'FRESH' if not changed and same_git and known.get('snapshot_id')==current.get('snapshot_id') else 'CHANGED','known_snapshot_id':known.get('snapshot_id',''),'current_snapshot_id':current.get('snapshot_id',''),'changed_file_count':len(changed),'changed_paths':changed}

def snapshot_registry_root(workspace:str|Path)->Path:
 return Path(workspace).expanduser().resolve()/'.agents'/'project_context'/'snapshots'

def save_project_snapshot(snapshot:dict)->Path:
 workspace=snapshot.get('workspace_root','');sid=snapshot.get('snapshot_id','')
 if not workspace or not sid: raise ValueError('snapshot_missing_workspace_or_id')
 root=snapshot_registry_root(workspace);root.mkdir(parents=True,exist_ok=True);path=root/f'{sid}.json';tmp=path.with_name(path.name+'.tmp')
 tmp.write_text(json.dumps(snapshot,ensure_ascii=False,indent=2,sort_keys=True),encoding='utf-8');tmp.replace(path)
 current=root.parent/'current.json';ctmp=current.with_name(current.name+'.tmp')
 ctmp.write_text(json.dumps({'snapshot_id':sid,'path':str(path),'workspace_root':workspace},ensure_ascii=False,indent=2,sort_keys=True),encoding='utf-8');ctmp.replace(current)
 return path

def load_project_snapshot(workspace:str|Path,snapshot_id:str)->dict:
 path=snapshot_registry_root(workspace)/f'{snapshot_id}.json'
 try:
  data=json.loads(path.read_text(encoding='utf-8'));return data if isinstance(data,dict) else {}
 except Exception:return {}

def load_current_project_snapshot(workspace:str|Path)->dict:
 root=snapshot_registry_root(workspace);index=root.parent/'current.json'
 try:
  meta=json.loads(index.read_text(encoding='utf-8'));sid=str(meta.get('snapshot_id',''))
 except Exception:return {}
 return load_project_snapshot(workspace,sid) if sid else {}
