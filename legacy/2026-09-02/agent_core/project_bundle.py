#!/usr/bin/env python3
from __future__ import annotations
import base64,hashlib,json,re
from pathlib import Path
from .project_sync import inspect_project_scope,load_project_snapshot,save_project_snapshot

TEXT_EXTS={'.c','.cc','.cpp','.cxx','.h','.hpp','.java','.kt','.kts','.py','.cmake','.txt','.md','.xml','.json','.js','.ts','.rs','.go'}

def dependency_evidence(workspace:str|Path,records:list[dict])->dict:
 root=Path(workspace).resolve();out=[];warnings=[]
 for rec in records:
  rel=rec['path'];p=root/rel
  if p.suffix.lower() not in TEXT_EXTS and p.name!='CMakeLists.txt': continue
  try:text=p.read_text(encoding='utf-8',errors='replace')
  except Exception as e:warnings.append({'path':rel,'warning':type(e).__name__});continue
  for i,line in enumerate(text.splitlines(),1):
   m=re.match(r'\s*#\s*include\s*[<"]([^>"]+)',line)
   if m:out.append({'source':rel,'relation':'include','target':m.group(1),'evidence_line':i})
   m=re.match(r'\s*import\s+([A-Za-z0-9_.*]+)',line)
   if m:out.append({'source':rel,'relation':'import','target':m.group(1),'evidence_line':i})
   m=re.search(r'target_link_libraries\s*\(([^)]*)\)',line,re.I)
   if m:out.append({'source':rel,'relation':'cmake_target_link_libraries','target':' '.join(m.group(1).split()),'evidence_line':i})
   m=re.search(r'add_(?:library|executable)\s*\(([^)]*)\)',line,re.I)
   if m:out.append({'source':rel,'relation':'cmake_target_definition','target':' '.join(m.group(1).split()),'evidence_line':i})
 return {'schema':'DEPENDENCY_EVIDENCE_V1','records':sorted(out,key=lambda x:(x['source'],x['evidence_line'],x['relation'],x['target'])),'warnings':sorted(warnings,key=lambda x:x['path'])}

def _encode_payload(raw:bytes)->tuple[str,bytes]:
 try: raw.decode('utf-8');return 'utf-8',raw
 except UnicodeDecodeError:return 'base64',base64.b64encode(raw)

def _file_block(root:Path,rec:dict)->bytes:
 raw=(root/rec['path']).read_bytes();encoding,payload=_encode_payload(raw)
 actual=hashlib.sha256(raw).hexdigest()
 if rec.get('sha256') and rec['sha256']!=actual: raise ValueError(f'source_hash_changed:{rec["path"]}')
 header=(f"FILE_BEGIN\npath: {rec['path']}\nlanguage: {rec.get('language',rec.get('extension',''))}\nsize_bytes: {len(raw)}\nsha256: {actual}\ncomplete=true\npayload_encoding: {encoding}\npayload_bytes: {len(payload)}\n").encode('utf-8')
 trailer=(f"\nFILE_END\npath: {rec['path']}\n").encode('utf-8')
 return header+payload+trailer

def reconstruct_bundle(path:str|Path)->dict[str,bytes]:
 data=Path(path).read_bytes();pos=0;out={}
 while pos<len(data):
  start=data.find(b'FILE_BEGIN\n',pos)
  if start<0:break
  marker=b'payload_bytes: ';line=data.find(marker,start);endline=data.find(b'\n',line)
  if line<0 or endline<0:raise ValueError('invalid_bundle_header')
  n=int(data[line+len(marker):endline]);header=data[start:endline+1].decode('utf-8');fields={}
  for row in header.splitlines()[1:]:
   if ': ' in row:k,v=row.split(': ',1);fields[k]=v
  payload=data[endline+1:endline+1+n];raw=base64.b64decode(payload) if fields.get('payload_encoding')=='base64' else payload
  if len(raw)!=int(fields['size_bytes']) or hashlib.sha256(raw).hexdigest()!=fields['sha256']:raise ValueError(f'bundle_hash_mismatch:{fields.get("path","")}')
  out[fields['path']]=raw;pos=endline+1+n
 return out

def build_source_bundles(workspace:str|Path,records:list[dict],output_dir:str|Path,max_bytes:int=500000,max_files:int=50,max_bundles:int=8)->dict:
 root=Path(workspace).resolve();outdir=Path(output_dir).resolve();outdir.mkdir(parents=True,exist_ok=True)
 bundles=[];current=[];size=0
 def flush():
  nonlocal current,size
  if not current:return
  idx=len(bundles)+1;body=b'\n'.join(x[1] for x in current);path=outdir/f'source_part_{idx:03d}.bundle.txt';path.write_bytes(body)
  reconstructed=reconstruct_bundle(path)
  expected={x[0] for x in current}
  if set(reconstructed)!=expected:raise ValueError('bundle_completeness_mismatch')
  bundles.append({'path':str(path),'bundle_index':idx,'file_count':len(current),'bytes':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'source_paths':sorted(expected)})
  current=[];size=0
 for rec in records:
  block=_file_block(root,rec);b=len(block)
  if int(rec.get('size_bytes',0))>max_bytes:raise ValueError(f'source_file_exceeds_bundle_limit:{rec["path"]}')
  if current and (len(current)>=max_files or size+b>max_bytes):flush()
  current.append((rec['path'],block));size+=b
 flush()
 if len(bundles)>max_bundles:raise ValueError(f'attachment_limit_exceeded:{len(bundles)}>{max_bundles}')
 return {'schema':'PROJECT_SOURCE_BUNDLE_V1','bundle_count':len(bundles),'bundles':bundles,'source_file_count':len(records),'complete':sum(x['file_count'] for x in bundles)==len(records)}

def delta_records(base:dict,current:dict)->dict:
 old={r['path']:r for r in base.get('files',[])};new={r['path']:r for r in current.get('files',[])}
 added=sorted(set(new)-set(old));deleted=sorted(set(old)-set(new));modified=sorted(p for p in set(old)&set(new) if old[p].get('sha256')!=new[p].get('sha256'))
 return {'schema':'PROJECT_DELTA_V1','base_snapshot_id':base.get('snapshot_id',''),'current_snapshot_id':current.get('snapshot_id',''),'added':added,'modified':modified,'deleted':deleted,'changed_paths':sorted(added+modified+deleted),'changed_file_count':len(added)+len(modified)+len(deleted)}

def build_project_delta(workspace:str|Path,base_snapshot_id:str,output_dir:str|Path|None=None,max_bytes:int=500000,max_files:int=50)->dict:
 root=Path(workspace).resolve();base=load_project_snapshot(root,base_snapshot_id)
 if not base:return {'schema':'PROJECT_DELTA_BUNDLE_V1','status':'DELTA_NOT_AVAILABLE','base_snapshot_id':base_snapshot_id}
 current=inspect_project_scope(root);delta=delta_records(base,current);changed=set(delta['added']+delta['modified'])
 records=[r for r in current.get('files',[]) if r['path'] in changed]
 outdir=Path(output_dir).resolve() if output_dir else root/'.agents'/'project_context'/'deltas'/f'{base_snapshot_id[:12]}-{current["snapshot_id"][:12]}'
 bundle=build_source_bundles(root,records,outdir,max_bytes=max_bytes,max_files=max_files)
 tombstones=[{'path':p,'deleted':True} for p in delta['deleted']]
 save_project_snapshot(current)
 return {'schema':'PROJECT_DELTA_BUNDLE_V1','status':'READY','base_snapshot_id':base_snapshot_id,'current_snapshot_id':current['snapshot_id'],'delta':delta,'bundle':bundle,'tombstones':tombstones}

def verify_delta_replay(base:dict,current:dict,delta:dict)->dict:
 old={r['path']:r.get('sha256') for r in base.get('files',[])};expected={r['path']:r.get('sha256') for r in current.get('files',[])};replayed=dict(old)
 current_map={r['path']:r.get('sha256') for r in current.get('files',[])}
 for p in delta.get('deleted',[]):replayed.pop(p,None)
 for p in delta.get('added',[])+delta.get('modified',[]):replayed[p]=current_map.get(p)
 mismatches=sorted(p for p in set(replayed)|set(expected) if replayed.get(p)!=expected.get(p))
 return {'status':'PASS' if not mismatches else 'FAIL','mismatches':mismatches,'reconstructed_file_count':len(replayed),'expected_file_count':len(expected)}
