#!/usr/bin/env python3
from __future__ import annotations
import base64,hashlib,json,re
from collections import Counter
from pathlib import Path
from .attachment_policy import AttachmentPolicy,resolve_attachment_policy
from .project_artifact_policy import INLINE_OR_CHUNK,artifact_policy_manifest,policy_for_record
from .project_sync import inspect_project_scope,load_project_snapshot,save_project_snapshot

TEXT_EXTS={'.c','.cc','.cpp','.cxx','.h','.hpp','.java','.kt','.kts','.py','.cmake','.txt','.md','.xml','.json','.js','.ts','.rs','.go'}
PROJECT_SYNC_RECEIPT_SCHEMA='PSR1'
PROJECT_SYNC_RECEIPT_RESERVE_BYTES=160

def _bundle_receipt(bundle_index:int,body:bytes)->tuple[str,bytes]:
 body_sha=hashlib.sha256(body).hexdigest()
 token='PSR1-'+hashlib.sha256(
  f'project-sync-receipt-v1\0{int(bundle_index)}\0{body_sha}'.encode('utf-8')
 ).hexdigest()[:24].upper()
 header=f'PROJECT_SYNC_RECEIPT_BEGIN {int(bundle_index)} {token}\n'.encode('ascii')
 footer=f'\nPROJECT_SYNC_RECEIPT_END {int(bundle_index)} {token}\n'.encode('ascii')
 return token,header+body+footer

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

def _chunk_utf8(raw:bytes,payload_budget:int)->list[bytes]:
 if len(raw)<=payload_budget:return [raw]
 try:raw.decode('utf-8')
 except UnicodeDecodeError:
  return [raw[i:i+max(1,int(payload_budget*0.70))] for i in range(0,len(raw),max(1,int(payload_budget*0.70)))]
 out=[];start=0
 while start<len(raw):
  end=min(len(raw),start+payload_budget)
  if end<len(raw):
   while end>start:
    try:raw[start:end].decode('utf-8');break
    except UnicodeDecodeError:end-=1
  if end<=start:end=min(len(raw),start+payload_budget)
  out.append(raw[start:end]);start=end
 return out

def _file_blocks(root:Path,rec:dict,max_block_bytes:int)->list[tuple[str,bytes,str]]:
 raw=(root/rec['path']).read_bytes();actual=hashlib.sha256(raw).hexdigest()
 if rec.get('sha256') and rec['sha256']!=actual: raise ValueError(f'source_hash_changed:{rec["path"]}')

 # Preserve the compact legacy framing whenever one complete file already fits.
 # This keeps very small synthetic transport ceilings (for example 400 bytes in
 # compatibility tests) viable and avoids paying chunk metadata overhead when
 # no chunking is required.
 encoding,payload=_encode_payload(raw)
 compact_header=(f"FILE_BEGIN\npath: {rec['path']}\nlanguage: {rec.get('language',rec.get('extension',''))}\nartifact_class: {rec.get('artifact_class','')}\nsize_bytes: {len(raw)}\nsha256: {actual}\ncomplete=true\npayload_encoding: {encoding}\npayload_bytes: {len(payload)}\n").encode('utf-8')
 compact_trailer=(f"\nFILE_END\npath: {rec['path']}\n").encode('utf-8')
 compact_block=compact_header+payload+compact_trailer
 if len(compact_block)<=max_block_bytes:
  return [(rec['path'],compact_block,rec['path'])]

 # The class ceiling decides whether the file itself is legal; max_block_bytes
 # is only the transport ceiling.  Find the largest practical payload budget
 # whose framed chunks stay inside that transport ceiling.
 payload_budget=max(1,max_block_bytes-1024)
 while True:
  chunks=_chunk_utf8(raw,payload_budget);count=len(chunks);blocks=[];offset=0;fits=True
  for index,chunk in enumerate(chunks,1):
   chunk_encoding,chunk_payload=_encode_payload(chunk);chunk_sha=hashlib.sha256(chunk).hexdigest()
   header=(f"FILE_BEGIN\npath: {rec['path']}\nlanguage: {rec.get('language',rec.get('extension',''))}\nartifact_class: {rec.get('artifact_class','')}\nsize_bytes: {len(raw)}\nsha256: {actual}\ncomplete=false\nfile_chunk_index: {index}\nfile_chunk_count: {count}\nchunk_offset_bytes: {offset}\nchunk_size_bytes: {len(chunk)}\nchunk_sha256: {chunk_sha}\npayload_encoding: {chunk_encoding}\npayload_bytes: {len(chunk_payload)}\n").encode('utf-8')
   trailer=(f"\nFILE_END\npath: {rec['path']}\n").encode('utf-8')
   block=header+chunk_payload+trailer
   if len(block)>max_block_bytes:
    fits=False;break
   blocks.append((f"{rec['path']}#chunk:{index}/{count}",block,rec['path']));offset+=len(chunk)
  if fits:return blocks
  if payload_budget<=1:raise ValueError(f'source_chunk_framing_exceeds_bundle_limit:{rec["path"]}')
  payload_budget=max(1,int(payload_budget*0.75))

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
  chunk_index=int(fields.get('file_chunk_index','1'));chunk_count=int(fields.get('file_chunk_count','1'));expected_size=int(fields.get('chunk_size_bytes',fields['size_bytes']));expected_sha=fields.get('chunk_sha256',fields['sha256'])
  if len(raw)!=expected_size or hashlib.sha256(raw).hexdigest()!=expected_sha:raise ValueError(f'bundle_hash_mismatch:{fields.get("path","")}')
  key=fields['path'] if chunk_count==1 else f"{fields['path']}#chunk:{chunk_index}/{chunk_count}"
  out[key]=raw;pos=endline+1+n
 return out

def _manifest_only_entry(rec:dict,policy,reason:str)->dict:
 return {'path':rec['path'],'artifact_class':policy.classification,'size_bytes':int(rec.get('size_bytes',0)),'sha256':rec.get('sha256',''),'sync_mode':policy.sync_mode,'max_single_file_bytes':policy.max_single_file_bytes,'reason':reason}

def build_source_bundles(workspace:str|Path,records:list[dict],output_dir:str|Path,max_bytes:int|None=None,max_files:int|None=None,policy:AttachmentPolicy|None=None)->dict:
 limits=resolve_attachment_policy(policy,max_files=max_files,max_bytes=max_bytes)
 max_bytes=limits.max_bytes_per_bundle;max_files=limits.max_files_per_bundle
 if max_bytes<=PROJECT_SYNC_RECEIPT_RESERVE_BYTES:raise ValueError('bundle_limit_too_small_for_receipt_envelope')
 payload_limit=max_bytes-PROJECT_SYNC_RECEIPT_RESERVE_BYTES
 root=Path(workspace).resolve();outdir=Path(output_dir).resolve();outdir.mkdir(parents=True,exist_ok=True)
 bundles=[];current=[];size=0;manifest_only=[];materialized=[];class_counts=Counter();materialized_counts=Counter();manifest_counts=Counter()
 def flush():
  nonlocal current,size
  if not current:return
  idx=len(bundles)+1;body=b'\n'.join(x[1] for x in current);receipt_token,enveloped=_bundle_receipt(idx,body);path=outdir/f'source_part_{idx:03d}.bundle.txt';path.write_bytes(enveloped)
  if path.stat().st_size>max_bytes:raise ValueError('bundle_receipt_envelope_exceeds_bundle_limit')
  reconstructed=reconstruct_bundle(path);expected={x[0] for x in current}
  if set(reconstructed)!=expected:raise ValueError('bundle_completeness_mismatch')
  logical_paths=sorted({x[2] for x in current})
  bundles.append({'path':str(path),'bundle_index':idx,'file_count':len(current),'bytes':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'receipt_token':receipt_token,'source_paths':logical_paths})
  current=[];size=0
 for rec in records:
  artifact_policy=policy_for_record(root,rec);class_counts[artifact_policy.classification]+=1
  file_size=int(rec.get('size_bytes',0))
  if file_size>artifact_policy.max_single_file_bytes:
   if artifact_policy.sync_mode==INLINE_OR_CHUNK:
    raise ValueError(f'project_file_exceeds_class_limit:{artifact_policy.classification}:{rec["path"]}')
   manifest_only.append(_manifest_only_entry(rec,artifact_policy,'class_limit_exceeded'));manifest_counts[artifact_policy.classification]+=1;continue
  if artifact_policy.sync_mode!=INLINE_OR_CHUNK:
   manifest_only.append(_manifest_only_entry(rec,artifact_policy,'manifest_only_by_policy'));manifest_counts[artifact_policy.classification]+=1;continue
  blocks=_file_blocks(root,rec,payload_limit)
  for block_id,block,logical_path in blocks:
   b=len(block);separator=1 if current else 0
   if current and (len(current)>=max_files or size+separator+b>payload_limit):flush();separator=0
   current.append((block_id,block,logical_path));size+=separator+b
  materialized.append(rec['path']);materialized_counts[artifact_policy.classification]+=1
 flush()
 if len(bundles)>limits.max_total_bundles:raise ValueError(f'total_bundle_safety_limit_exceeded:{len(bundles)}>{limits.max_total_bundles}')
 bundle_entry_count=sum(x['file_count'] for x in bundles);project_count=len(records);accounted=len(materialized)+len(manifest_only)
 return {'schema':'PROJECT_SOURCE_BUNDLE_V2','bundle_count':len(bundles),'bundles':bundles,'source_file_count':len(materialized),'bundle_entry_count':bundle_entry_count,'project_file_count':project_count,'manifest_only_file_count':len(manifest_only),'manifest_only_files':manifest_only,'materialized_paths':sorted(materialized),'complete':accounted==project_count,'class_counts':dict(sorted(class_counts.items())),'materialized_class_counts':dict(sorted(materialized_counts.items())),'manifest_only_class_counts':dict(sorted(manifest_counts.items())),'artifact_policy':artifact_policy_manifest(),'policy':{'max_files_per_bundle':limits.max_files_per_bundle,'max_bytes_per_bundle':limits.max_bytes_per_bundle,'max_attachments_per_message':limits.max_attachments_per_message,'max_batches':limits.max_batches,'max_total_bundles':limits.max_total_bundles}}

def delta_records(base:dict,current:dict)->dict:
 old={r['path']:r for r in base.get('files',[])};new={r['path']:r for r in current.get('files',[])}
 added=sorted(set(new)-set(old));deleted=sorted(set(old)-set(new));modified=sorted(p for p in set(old)&set(new) if old[p].get('sha256')!=new[p].get('sha256'))
 return {'schema':'PROJECT_DELTA_V1','base_snapshot_id':base.get('snapshot_id',''),'current_snapshot_id':current.get('snapshot_id',''),'added':added,'modified':modified,'deleted':deleted,'changed_paths':sorted(added+modified+deleted),'changed_file_count':len(added)+len(modified)+len(deleted)}

def build_project_delta(workspace:str|Path,base_snapshot_id:str,output_dir:str|Path|None=None,max_bytes:int|None=None,max_files:int|None=None,policy:AttachmentPolicy|None=None)->dict:
 root=Path(workspace).resolve();base=load_project_snapshot(root,base_snapshot_id)
 if not base:return {'schema':'PROJECT_DELTA_BUNDLE_V1','status':'DELTA_NOT_AVAILABLE','base_snapshot_id':base_snapshot_id}
 current=inspect_project_scope(root);delta=delta_records(base,current);changed=set(delta['added']+delta['modified'])
 records=[r for r in current.get('files',[]) if r['path'] in changed]
 outdir=Path(output_dir).resolve() if output_dir else root/'.agents'/'project_context'/'deltas'/f'{base_snapshot_id[:12]}-{current["snapshot_id"][:12]}'
 bundle=build_source_bundles(root,records,outdir,max_bytes=max_bytes,max_files=max_files,policy=policy)
 tombstones=[{'path':p,'deleted':True} for p in delta['deleted']];save_project_snapshot(current)
 return {'schema':'PROJECT_DELTA_BUNDLE_V1','status':'READY','base_snapshot_id':base_snapshot_id,'current_snapshot_id':current['snapshot_id'],'delta':delta,'bundle':bundle,'tombstones':tombstones}

def verify_delta_replay(base:dict,current:dict,delta:dict)->dict:
 old={r['path']:r.get('sha256') for r in base.get('files',[])};expected={r['path']:r.get('sha256') for r in current.get('files',[])};replayed=dict(old);current_map={r['path']:r.get('sha256') for r in current.get('files',[])}
 for p in delta.get('deleted',[]):replayed.pop(p,None)
 for p in delta.get('added',[])+delta.get('modified',[]):replayed[p]=current_map.get(p)
 mismatches=sorted(p for p in set(replayed)|set(expected) if replayed.get(p)!=expected.get(p))
 return {'status':'PASS' if not mismatches else 'FAIL','mismatches':mismatches,'reconstructed_file_count':len(replayed),'expected_file_count':len(expected)}
