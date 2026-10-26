"""Native sandbox regressions. Never operates on an installed Runtime."""
from pathlib import Path
import hashlib
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from agent_core import force_stop_all_agents as stop
from agent_core import force_stop_cleanup as cleanup
from agent_core.chunked_write import ChunkedWriteManager
from agent_core.json_state_io import write_json_atomic
from agent_core.paths import remote_tasks_path, remote_binding_transactions_path
from agent_core.remote_clean_start import remote_clean_start
from agent_core.task_state import TaskRecord, TaskStateStore


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree(root):
    return {str(p.relative_to(root)): digest(p) for p in root.rglob('*') if p.is_file()}


def data_tree(root):
    # Kernel-lock bookkeeping may be created on an attempted real cleanup.
    # Protected task/writer/config data must remain byte-for-byte unchanged.
    return {k:v for k,v in tree(root).items() if not k.endswith(('.lock','.lock.v2'))}


class OwnedCleanup(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='force-stop-native-')
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / 'workspace'; self.workspace.mkdir()
        self.store = TaskStateStore(remote_tasks_path(self.root))

    def tearDown(self):
        self.temp.cleanup()

    def task(self, n=1, state='PAUSED', transport='LOCAL_TEST'):
        row = TaskRecord(task_id='TASK-'+f'{n:020X}', request_id='RR-TEST-'+str(n),
                         task_epoch='epoch-'+str(n), workspace=str(self.workspace),
                         state=state, metadata={'transport': transport})
        self.store.put(row)
        return row

    def writer(self, task, *, legacy=False):
        key = hashlib.sha256(json.dumps([task.task_id, task.task_epoch, task.request_id],
                             separators=(',', ':')).encode()).hexdigest()
        state = self.workspace / '.agents/chunked_writes'
        if not legacy: state = state / 'tasks' / key
        manager = ChunkedWriteManager(state, [self.workspace])
        write_id='WRITE-'+task.task_id
        manager.begin({'tool':'begin_file_write','action_id':'begin-'+write_id,
                       'write_id':write_id,'path':str(self.workspace / 'destination.txt')},
                      request_id=task.request_id,run_id=task.request_id)
        manager.write_chunk({'tool':'write_file_chunk','action_id':'chunk-'+write_id,
                             'write_id':write_id,'content':'中文 staged only\n'})
        manifest=manager._manifest_path(write_id)
        data=json.loads(manifest.read_text());data['lease_owner']='pid:987654321'
        write_json_atomic(manifest,data) # Isolated fixture simulates exited owner.
        return manifest,Path(data['staging_path'])

    def clean(self, inventory=None):
        return cleanup.cleanup_stopped_work(self.root, inventory or [], inventory_provider=lambda: inventory or [])

    def test_all_five_states_cancelled_and_archived_not_success(self):
        rows=[self.task(i,s) for i,s in enumerate(sorted(cleanup.INCOMPLETE),1)]
        original=remote_tasks_path(self.root).read_bytes()
        result=self.clean();self.assertTrue(result['success'],result)
        current=TaskStateStore(remote_tasks_path(self.root)).load()
        self.assertEqual({t.state for t in current.values()},{'CANCELLED'})
        self.assertEqual(len(result['tasks_cancelled']),5)
        archive=Path(result['archive']); index=json.loads((archive/'index.json').read_text())
        entry=next(x for x in index['files'] if x['source']==str(remote_tasks_path(self.root)))
        self.assertEqual((archive/entry['file']).read_bytes(),original)
        for row in rows:
            self.assertEqual(current[row.task_id].metadata['force_stop']['previous_state'],row.state)

    def test_real_open_chunk_archived_before_abort_target_absent(self):
        task=self.task();manifest,stage=self.writer(task);raw=stage.read_bytes();before=manifest.read_bytes()
        result=self.clean();self.assertTrue(result['success'],result)
        self.assertEqual(json.loads(manifest.read_text())['state'],'ABORTED')
        self.assertFalse(stage.exists());self.assertFalse((self.workspace/'destination.txt').exists())
        archive=Path(result['archive']);index=json.loads((archive/'index.json').read_text())
        copied={x['source']:(archive/x['file']).read_bytes() for x in index['files']}
        self.assertEqual(copied[str(stage)],raw);self.assertEqual(copied[str(manifest)],before)
        self.assertEqual(len(result['writers_aborted']),1)

    def test_repeat_stop_is_idempotent(self):
        task=self.task();manifest,_=self.writer(task)
        self.assertTrue(self.clean()['success']);raw=manifest.read_bytes()
        result=self.clean();self.assertTrue(result['success'],result)
        self.assertEqual(result['tasks_cancelled'],[]);self.assertEqual(result['writers_aborted'],[])
        self.assertEqual(manifest.read_bytes(),raw)

    def test_normal_clean_start_preserves_paused(self):
        task=self.task();before=remote_tasks_path(self.root).read_bytes()
        with patch('agent_core.remote_clean_start._terminate_task_processes',return_value=0):
            remote_clean_start(self.root)
        self.assertEqual(remote_tasks_path(self.root).read_bytes(),before)

    def test_archive_failure_changes_no_task_or_writer(self):
        task=self.task();manifest,stage=self.writer(task);before=data_tree(self.root)
        with patch.object(cleanup,'_archive',side_effect=OSError('archive-disk-full')):
            result=self.clean()
        self.assertFalse(result['success']);self.assertEqual(data_tree(self.root),before)

    def test_committing_or_quarantined_or_unknown_preserved(self):
        for state in ['COMMITTING','QUARANTINED','UNKNOWN']:
            with self.subTest(state=state):
                task=self.task();manifest,_=self.writer(task)
                data=json.loads(manifest.read_text());data['state']=state;write_json_atomic(manifest,data)
                before=data_tree(self.root);result=self.clean()
                self.assertFalse(result['success']);self.assertEqual(data_tree(self.root),before)
                # Restore only disposable fixture to continue this test.
                import shutil;shutil.rmtree(self.workspace/'.agents')

    def test_foreign_legacy_open_is_untouched(self):
        own=self.task();foreign=TaskRecord(task_id='TASK-FOREIGN',request_id='RR-FOREIGN',task_epoch='other')
        manifest,stage=self.writer(foreign,legacy=True);before=(manifest.read_bytes(),stage.read_bytes())
        result=self.clean();self.assertFalse(result['success'],result)
        self.assertEqual((manifest.read_bytes(),stage.read_bytes()),before)

    def test_legacy_owned_open_is_aborted(self):
        task=self.task();manifest,_=self.writer(task,legacy=True)
        result=self.clean();self.assertTrue(result['success'],result)
        self.assertEqual(json.loads(manifest.read_text())['state'],'ABORTED')

    def test_orphan_task_scoped_open_blocks(self):
        task=self.task();manifest,stage=self.writer(task)
        orphan=manifest.parent.parent.with_name('a'*64)
        manifest.parent.parent.rename(orphan)
        before=data_tree(self.root);result=self.clean()
        self.assertFalse(result['success']);self.assertEqual(data_tree(self.root),before)

    def test_missing_manifest_with_orphan_bytes_blocks(self):
        self.task();orphan=self.workspace/'.agents/chunked_writes/tasks'/('b'*64)/'staging'
        orphan.mkdir(parents=True);(orphan/'orphan.part').write_bytes(b'unknown')
        before=data_tree(self.root);result=self.clean()
        self.assertFalse(result['success']);self.assertEqual(data_tree(self.root),before)

    def test_saved_execution_plan_cancelled_and_archived(self):
        task=self.task();plan_id='PLAN-'+'C'*20
        task.metadata['source_plan_id']=plan_id;self.store.put(task)
        path=remote_tasks_path(self.root).parent/'plan_ledger'/(plan_id+'.json')
        record={'schema':'SMARTAGENT_EXECUTION_PLAN_V1','plan_id':plan_id,
                'state':'EXECUTING','task_id':'TASK-ORIGINAL','request_id':'RR-ORIGINAL',
                'workspace':str(self.workspace),'original_request':'original plan',
                'final_summary':'plan summary','execution_task_id':task.task_id,
                'execution_request_id':task.request_id}
        write_json_atomic(path,record);original=path.read_bytes()
        result=self.clean();self.assertTrue(result['success'],result)
        self.assertEqual(json.loads(path.read_text())['state'],'CANCELLED')
        archive=Path(result['archive']);index=json.loads((archive/'index.json').read_text())
        entry=next(x for x in index['files'] if x['source']==str(path))
        self.assertEqual((archive/entry['file']).read_bytes(),original)

    def test_source_plan_owner_mismatch_blocks_before_abort(self):
        task=self.task();manifest,_=self.writer(task);plan_id='PLAN-'+'D'*20
        task.metadata['source_plan_id']=plan_id;self.store.put(task)
        path=remote_tasks_path(self.root).parent/'plan_ledger'/(plan_id+'.json')
        write_json_atomic(path,{'schema':'SMARTAGENT_EXECUTION_PLAN_V1','plan_id':plan_id,
                               'state':'EXECUTING','execution_task_id':'FOREIGN','execution_request_id':task.request_id})
        before=data_tree(self.root);result=self.clean()
        self.assertFalse(result['success']);self.assertEqual(data_tree(self.root),before)

    def test_new_agent_during_archive_blocks_before_mutation(self):
        task=self.task();manifest,stage=self.writer(task)
        before=(remote_tasks_path(self.root).read_bytes(),manifest.read_bytes(),stage.read_bytes())
        started=[{'ProcessId':123,'ParentProcessId':0,'CommandLine':f'python -m agent_core.host_supervisor --root "{self.root}"'}]
        with patch.object(cleanup,'_powershell_process_inventory',create=True):
            result=cleanup.cleanup_stopped_work(self.root,[],inventory_provider=unittest.mock.Mock(side_effect=[[],started]))
        self.assertFalse(result['success'])
        self.assertEqual((remote_tasks_path(self.root).read_bytes(),manifest.read_bytes(),stage.read_bytes()),before)

    def test_empty_state_still_checks_fresh_processes(self):
        started=[{'ProcessId':123,'ParentProcessId':0,'CommandLine':f'python -m agent_core.runtime_cleanup prepare --root "{self.root}"'}]
        result=cleanup.cleanup_stopped_work(self.root,[],inventory_provider=lambda: started)
        self.assertFalse(result['success']);self.assertFalse(remote_tasks_path(self.root).exists())

    def test_unlink_failure_retries_archived_abort(self):
        task=self.task();manifest,stage=self.writer(task);original=Path.unlink
        def locked(path,*args,**kwargs):
            if path==stage:raise PermissionError('fixture staging temporarily locked')
            return original(path,*args,**kwargs)
        with patch.object(Path,'unlink',locked):result=self.clean()
        self.assertFalse(result['success']);self.assertTrue(stage.exists())
        self.assertEqual(json.loads(manifest.read_text())['state'],'ABORTED')
        self.assertEqual(TaskStateStore(remote_tasks_path(self.root)).load()[task.task_id].state,'PAUSED')
        again=self.clean();self.assertTrue(again['success'],again);self.assertFalse(stage.exists())

    def test_event_failure_after_cancel_is_reconciled_on_retry(self):
        task=self.task()
        with patch.object(cleanup.RemoteEventStore,'pause_for_tasks',side_effect=OSError('fixture event store unavailable')):
            result=self.clean()
        self.assertFalse(result['success'])
        row=TaskStateStore(remote_tasks_path(self.root)).load()[task.task_id]
        self.assertEqual(row.state,'CANCELLED');self.assertFalse(row.metadata['force_stop']['cleanup_complete'])
        again=self.clean();self.assertTrue(again['success'],again)
        row=TaskStateStore(remote_tasks_path(self.root)).load()[task.task_id]
        self.assertTrue(row.metadata['force_stop']['cleanup_complete'])

    def test_plan_failure_after_cancel_is_reconciled_on_retry(self):
        from agent_core.plan_ledger import PlanLedger
        task=self.task();plan_id='PLAN-'+'E'*20
        task.metadata['source_plan_id']=plan_id;self.store.put(task)
        path=remote_tasks_path(self.root).parent/'plan_ledger'/(plan_id+'.json')
        write_json_atomic(path,{'schema':'SMARTAGENT_EXECUTION_PLAN_V1','plan_id':plan_id,
                'state':'EXECUTING','task_id':'TASK-PLAN','request_id':'RR-PLAN',
                'workspace':str(self.workspace),'original_request':'plan','final_summary':'summary',
                'execution_task_id':task.task_id,'execution_request_id':task.request_id})
        with patch.object(PlanLedger,'finish_execution_task',side_effect=RuntimeError('fixture plan store unavailable')):
            result=self.clean()
        self.assertFalse(result['success']);self.assertEqual(json.loads(path.read_text())['state'],'EXECUTING')
        again=self.clean();self.assertTrue(again['success'],again)
        self.assertEqual(json.loads(path.read_text())['state'],'CANCELLED')

    def test_foreign_raw_task_fields_retained(self):
        self.task();foreign=self.task(2,state='COMPLETED',transport='FOREIGN')
        path=remote_tasks_path(self.root);payload=json.loads(path.read_text())
        payload['tasks'][foreign.task_id]['future_field']={'do_not_lose':'value'}
        write_json_atomic(path,payload);before=payload['tasks'][foreign.task_id]
        result=self.clean();self.assertTrue(result['success'],result)
        self.assertEqual(json.loads(path.read_text())['tasks'][foreign.task_id],before)

    def test_ambiguous_request_identity_is_blocked(self):
        a=self.task(1);b=self.task(2);b.request_id=a.request_id;self.store.put(b)
        before=data_tree(self.root);result=self.clean()
        self.assertFalse(result['success']);self.assertEqual(data_tree(self.root),before)

    def test_binding_transactions_invalidated_settings_preserved(self):
        self.task();path=remote_binding_transactions_path(self.root)
        write_json_atomic(path,{'version':1,'transactions':{'WWM-TEST':{'chat_id':1}}})
        settings=self.root/'localdata/bindings/remote_primary_binding.json'
        settings.parent.mkdir(parents=True,exist_ok=True);settings.write_bytes(b'{"settings":"KEEP"}')
        result=self.clean();self.assertTrue(result['success'],result)
        self.assertEqual(result['transactions_invalidated'],1)
        self.assertEqual(json.loads(path.read_text())['transactions'],{})
        self.assertEqual(settings.read_bytes(),b'{"settings":"KEEP"}')

    def test_live_or_reused_metadata_pid_blocks_without_kill(self):
        task=self.task();task.metadata['worker_pid']=123;self.store.put(task);before=data_tree(self.root)
        result=self.clean([{'ProcessId':123,'ParentProcessId':0,'CommandLine':'unrelated editor'}])
        self.assertFalse(result['success']);self.assertEqual(data_tree(self.root),before)

    def test_live_excluded_agent_blocks_owned_cleanup(self):
        self.task();before=tree(self.root)
        inventory=[{'ProcessId':123,'ParentProcessId':0,'CommandLine':f'python -m agent_core.host_supervisor --root "{self.root}"'}]
        result=self.clean(inventory)
        self.assertFalse(result['success']);self.assertEqual(tree(self.root),before)

    def test_foreign_process_root_prefix_not_discovered(self):
        row={'ProcessId':123,'ParentProcessId':0,'CommandLine':f'python -m agent_core.host_supervisor --root "{self.root}2"'}
        self.assertEqual(stop.discover_agent_pids([row],self.root)['selected_pids'],[])

    def test_dry_run_byte_for_byte_no_cleanup_or_mkdir(self):
        task=self.task();self.writer(task);before=tree(self.root)
        with patch.object(stop,'_powershell_process_inventory',return_value=[]):
            result=stop.force_stop_all(self.root,dry_run=True)
        self.assertTrue(result['dry_run']);self.assertEqual(tree(self.root),before)

    def test_modified_archive_source_fails_before_mutation(self):
        task=self.task();manifest,stage=self.writer(task)
        original=cleanup._archive
        def race(root,files):
            result=original(root,files);stage.write_bytes(b'external drift');return result
        task_before=remote_tasks_path(self.root).read_bytes();manifest_before=manifest.read_bytes()
        with patch.object(cleanup,'_archive',side_effect=race):result=self.clean()
        self.assertFalse(result['success']);self.assertEqual(remote_tasks_path(self.root).read_bytes(),task_before)
        self.assertEqual(manifest.read_bytes(),manifest_before)

    def test_reparse_boundary_rejected(self):
        task=self.task();manifest,_=self.writer(task);before=data_tree(self.root)
        original=cleanup._plain
        def deny(path):
            if 'tasks' in Path(path).parts:raise RuntimeError('force_stop_reparse_rejected')
            return original(path)
        # Deterministic junction detection simulation; actual OS link case is
        # separately conditional on Windows link privileges.
        with patch.object(cleanup,'_plain',side_effect=deny):result=self.clean()
        # The descendant check covers all ancestors in production _plain.
        if result['success']:
            self.fail('injected reparse check was not reached')
        self.assertEqual(data_tree(self.root),before)


if __name__=='__main__':
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(OwnedCleanup)
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():raise SystemExit(1)
    print('FORCE_STOP_OWNED_CLEANUP_NATIVE_PASS')
