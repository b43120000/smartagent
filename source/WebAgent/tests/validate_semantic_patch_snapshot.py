"""T25 root-file exception must prove all non-artifact state unchanged."""
import hashlib,json,os,subprocess,tempfile
from pathlib import Path
from unittest.mock import patch
from agent_core.semantic_map import update_semantic_map,update_semantic_map_file,semantic_map_path
from WebAgent.tests.validate_semantic_map_matrix import payload

def fixture(parent,name,git=False,filename='semantic_patch.json'):
    root=parent/name;root.mkdir();(root/'a.py').write_bytes(b'A = 1\n');(root/'b.py').write_bytes(b'B = 2\n')
    if git:
        for args in (['init'],['add','a.py','b.py'],['-c','user.name=Matrix Test','-c','user.email=matrix@example.invalid','commit','-m','fixture']):
            subprocess.run(['git','-C',str(root),*args],check=True,capture_output=True)
    assert update_semantic_map(root,payload(root,['a.py','b.py']))['status']=='UPDATED'
    p=payload(root,['a.py']);p['files'][0]['responsibility']='Root patch applied'
    f=root/filename;f.write_text(json.dumps(p),encoding='utf-8')
    return root,f,p,hashlib.sha256(f.read_bytes()).hexdigest()

def run():
    with tempfile.TemporaryDirectory(prefix='t25-root-') as tmp:
        parent=Path(tmp)
        for name,git,filename in (('plain',False,'patch with space.json'),('git',True,'patch with space.json'),('unicode',True,'語意補丁.json')):
            root,f,p,digest=fixture(parent,'positive_'+name,git,filename=filename)
            before={n:(root/n).read_bytes() for n in ('a.py','b.py')}
            result=update_semantic_map_file(root,str(f),digest)
            assert result['status']=='UPDATED' and result['verification_status']=='PASS',result
            proof=result['patch_snapshot_evidence']
            assert proof['original_base_snapshot_id']==p['base_snapshot_id'] and proof['artifact_sha256']==digest
            assert all((root/n).read_bytes()==v for n,v in before.items())
        for mode in ('source_changed','extra_json','wrong_base','wrong_hash','race','late_race','existing_patch','git_head_changed','truncated_status','unknown_version'):
            root,f,p,digest=fixture(parent,mode,git=mode=='git_head_changed')
            original=semantic_map_path(root).read_bytes()
            if mode=='source_changed':
                file=root/'b.py';stamp=file.stat();file.write_bytes(b'B = 9\n');os.utime(file,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
            elif mode=='extra_json':(root/'other.json').write_bytes(b'{}')
            elif mode=='wrong_base':
                p['base_snapshot_id']='0'*64;f.write_text(json.dumps(p),encoding='utf-8');digest=hashlib.sha256(f.read_bytes()).hexdigest()
            elif mode=='wrong_hash':digest='0'*64
            elif mode=='existing_patch':
                p=payload(root,['a.py']);f.write_text(json.dumps(p),encoding='utf-8');digest=hashlib.sha256(f.read_bytes()).hexdigest()
            elif mode=='git_head_changed':
                subprocess.run(['git','-C',str(root),'-c','user.name=Matrix Test','-c','user.email=matrix@example.invalid','commit','--allow-empty','-m','changed head'],check=True,capture_output=True)
            if mode in ('truncated_status','unknown_version'):
                import agent_core.semantic_map as module
                inspect=module.inspect_project_scope
                def incomplete(*args,**kwargs):
                    snapshot=inspect(*args,**kwargs)
                    return {**snapshot,**({'git_status':['?? unknown']*200} if mode=='truncated_status' else {'schema_version':999})}
                with patch.object(module,'inspect_project_scope',side_effect=incomplete):
                    result=update_semantic_map_file(root,str(f),digest)
            elif mode=='late_race':
                import agent_core.semantic_map as module
                load=module._load;calls=[]
                def mutate_after_precheck(workspace):
                    calls.append(1)
                    if len(calls)==1:(root/'b.py').write_bytes(b'B = 9\n')
                    return load(workspace)
                with patch.object(module,'_load',side_effect=mutate_after_precheck):
                    result=update_semantic_map_file(root,str(f),digest)
                assert result['status']=='PARTIAL' and result['verification_status']=='FAIL' and result['mutation_applied'] is True,result
                assert result['reason']=='SOURCE_CHANGED_DURING_UPDATE'
                from agent_core.tool_result_policy import structured_outcome
                outcome=structured_outcome('update_semantic_map_file',result)
                assert outcome[0]=='PARTIAL' and not outcome[3]['terminal_eligible'],outcome
                continue
            elif mode=='race':
                def changed(workspace,value):
                    (root/'b.py').write_bytes(b'B = 9\n');return update_semantic_map(workspace,value)
                with patch('agent_core.semantic_map.update_semantic_map',side_effect=changed):
                    result=update_semantic_map_file(root,str(f),digest)
            else:result=update_semantic_map_file(root,str(f),digest)
            assert result['status'] in ('INVALID','SNAPSHOT_MISMATCH'),(mode,result)
            assert semantic_map_path(root).read_bytes()==original,mode
    print('SEMANTIC_PATCH_SNAPSHOT_PASS')

if __name__=='__main__':run()
