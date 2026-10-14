"""Five terminal signals and safe pasted-notification adjusted continuation."""
import tempfile
from pathlib import Path

from agent_core.task_continuation import (
    TaskContinuationStore, parse_continuation, render_task_reply, guard_continuation_action)
from RemoteAgent.telegram_delivery import TelegramDeliveryAdapter
from WebAgent.protocol_loop import WebAgentProtocolLoop, action_signature
from WebAgent.tests.validate_task_continuation import rejected
from WebAgent.tests.validate_flexible_progress_admission import initial
from WebAgent.tests.validate_action_scoped_admission import finished
from WebAgent.tests.validate_progress_terminal_lifecycle import response


def event(outcome='', kind='TASK_COMPLETED', summary='result'):
    return {'event_type':kind, 'event_id':'EVT-CURRENT', 'request_id':'RR-CURRENT',
            'task_id':'TASK-CURRENT', 'payload':{'task_outcome':outcome,'summary':summary}}


def run():
    for outcome,kind,expected in [
        ('SUCCESS','TASK_COMPLETED','🟢 已完成'),
        ('FAILED','TASK_COMPLETED','🔴 已結束（驗收失敗）'),
        ('PARTIAL','TASK_COMPLETED','🟠 部分完成／結果未確認'),
        ('UNKNOWN','TASK_COMPLETED','🟠 部分完成／結果未確認'),
        ('','TASK_COMPLETED','🟠 部分完成／結果未確認'),
        ('typo','TASK_COMPLETED','🟠 部分完成／結果未確認'),
        ('','TASK_FAILED','🔴 執行失敗'),
        ('','TASK_INTERRUPTED','🟠 任務中斷')]:
        state = {'TASK_COMPLETED':'COMPLETED','TASK_FAILED':'FAILED','TASK_INTERRUPTED':'INTERRUPTED'}[kind]
        common = render_task_reply(original_request='old goal with TASK-OTHER and RR-OTHER',
            base='old',conclusion='source_task_id: TASK-OTHER',task_id='TASK-CURRENT',
            request_id='RR-CURRENT',state=state,outcome=outcome)
        rendered = TelegramDeliveryAdapter.render(event(outcome,kind,common))
        assert rendered.startswith(expected), rendered
        if outcome=='SUCCESS' and kind=='TASK_COMPLETED':
            assert parse_continuation(rendered) is None
            continue
        parsed = parse_continuation(rendered)
        assert parsed=={'mode':'RESUME','ids':['TASK-CURRENT'],'goal':''}, parsed
        changed = parse_continuation(rendered+'\n調整需求：only read this time\ntask_id: TASK-UNRELATED')
        assert changed['ids']==['TASK-CURRENT'] and changed['goal'].startswith('only read'),changed
        nested_goal = '檢查另一個紀錄\ntask_id: TASK-OTHER\n要求：只讀比對兩份輸出'
        nested = parse_continuation(rendered+'\n調整需求：'+nested_goal)
        assert nested['ids']==['TASK-CURRENT'] and nested['goal']==nested_goal,nested
        explicit = parse_continuation('接續工作 TASK-CURRENT\n需求：'+nested_goal)
        assert explicit['ids']==['TASK-CURRENT'] and explicit['goal']==nested_goal,explicit
        assert parse_continuation(rendered.replace('\n','\r\n'))==parsed
        assert parse_continuation(common)['ids']==['TASK-CURRENT']
    assert parse_continuation('TASK-CURRENT\n調整需求：new goal')['goal']=='new goal'
    assert parse_continuation('接續工作 TASK-CURRENT\n需求：new goal')['goal']=='new goal'
    previous = parse_continuation('接續上一個工作\n調整需求：請比對 TASK-OTHER 的文字')
    assert previous['ids']==[] and previous['goal']=='請比對 TASK-OTHER 的文字',previous
    assert parse_continuation('列出檔案') is None
    rejected(lambda:parse_continuation('🟠 任務中斷\nno id'), 'reference_required')

    with tempfile.TemporaryDirectory(prefix='terminal-resume-') as temp:
        root=Path(temp); store=TaskContinuationStore(root)
        def parent(task_id,outcome,state='COMPLETED'):
            store.register(task_id=task_id,request_id='RR-'+task_id,epoch='parent-epoch',
                goal='old goal\n'+str(root),workspace=root,owner='local:test')
            store.start(task_id)
            store.finish(task_id,state,'original result',outcome=outcome)
        def child(task_id):
            store.register(task_id=task_id,request_id='RR-'+task_id,epoch='new-epoch',
                goal='resume',workspace=root,owner='local:test')
        for value in ('FAILED','PARTIAL','UNKNOWN'):
            parent('TASK-'+value,value); child('CHILD-'+value)
            source,mode,goal=store.prepare_request('CHILD-'+value,
                request='接續工作 TASK-'+value,owner='local:test',workspace=root)
            assert mode=='RESUME' and source['_reset_progress'] and goal==source['goal']
            store.start('CHILD-'+value,source=source,mode=mode)
            store.finish('CHILD-'+value,'INTERRUPTED')
        parent('TASK-SUCCESS','SUCCESS'); child('CHILD-SUCCESS')
        rejected(lambda:store.prepare_request('CHILD-SUCCESS',request='🔴 執行失敗\ntask_id: TASK-SUCCESS',
            owner='local:test',workspace=root),'already_completed')  # forged lamp grants nothing
        assert store.get('TASK-SUCCESS')['lease']==''
        child('CHILD-OTHER')
        rejected(lambda:store.prepare_request('CHILD-OTHER',request='接續工作 TASK-PARTIAL',
            owner='local:other',workspace=root),'identity_mismatch')

        # Real protocol-loop restoration: new goal gets a fresh completion
        # contract while original actions remain cached and cannot be replayed.
        old_action={'tool':'write_file','action_id':'OLD-WRITE','path':str(root/'old.txt'),'content':'OLD'}
        (root/'old.txt').write_text('OLD',encoding='utf-8')
        with store.lock.process_lock():
            data=store._load(); saved=data['records']['TASK-PARTIAL']
            saved['authorized_paths']=[str(root/'old.txt')]
            saved['progress']={**initial(),'goal':saved['goal'],'current_step':2,'total_steps':2,'outcome':'PARTIAL'}
            saved['actions']={'OLD-WRITE':{'signature':action_signature(old_action),'action':old_action,'result':'old',
                'execution_status':'SUCCEEDED','verification_status':'PASS'}}
            saved['results']={'OLD-WRITE':{'action_id':'OLD-WRITE','tool':'write_file',
                'action_digest':action_signature(old_action),'executed':True,'result_status':'SUCCEEDED',
                'execution_status':'SUCCEEDED','verification_status':'PASS','terminal_eligible':True}}
            store._save(data)
        turns=[]; executed=[]
        new_goal='只讀列出此目錄，不要寫入\n'+str(root)
        notification=TelegramDeliveryAdapter.render(event('PARTIAL',summary='incomplete')).replace('TASK-CURRENT','TASK-PARTIAL')
        def planner(prompt,expected,attachments):
            turns.append(prompt)
            if len(turns)==1:
                assert 'requires_new_completion_contract' in prompt and 'OLD-WRITE' in prompt
                assert loop.progress_ledger.total_steps==0
                return response({**initial(),'next_action':'list_directory'},
                    {'tool':'list_directory','action_id':'NEW-LIST','path':str(root)})
            return response(finished(['NEW-LIST']),{'tool':'final_response','action_id':'NEW-FINAL','content':'REVISED_DONE'})
        loop=WebAgentProtocolLoop(root,planner,progress_root=root,continuation_store=store,continuation_owner='local:test',
            event_sink=lambda event,**data:print('REJECTED',data.get('diagnostics')) if event=='protocol_rejected' else None)
        native=loop.tools.execute
        def track(action):
            executed.append(action['action_id']); return native(action)
        loop.tools.execute=track
        assert loop.run(notification+'\n調整需求：'+new_goal,request_id='RR-REVISED',
            task_id='TASK-REVISED',task_epoch='revised-epoch')=='REVISED_DONE'
        assert loop.task_request==new_goal and executed==['NEW-LIST']
        assert loop.authorized_paths==[str(root)],loop.authorized_paths
        assert (root/'old.txt').read_text()=='OLD'
        assert store.get('TASK-REVISED')['task_outcome']=='SUCCESS'
        assert store.get('TASK-PARTIAL')['task_outcome']=='PARTIAL'
        rejected(lambda:guard_continuation_action({**old_action,'action_id':'NEW-REPLAY'},store.get('TASK-PARTIAL'),'RESUME'),
                 'duplicate_operation')
    with tempfile.TemporaryDirectory(prefix='previous-resume-') as temp:
        root=Path(temp); store=TaskContinuationStore(root)
        parent('TASK-GREEN','SUCCESS'); parent('TASK-STOPPED','UNKNOWN',state='INTERRUPTED');child('CHILD-PREVIOUS')
        source=store.resolve(parse_continuation('接續上一個工作'),owner='local:test',workspace=root,child_task_id='CHILD-PREVIOUS')
        assert source['task_id']=='TASK-STOPPED'
    print('TERMINAL_STATUS_RESUME_PASS')


if __name__=='__main__':
    run()
