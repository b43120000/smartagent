"""2A isolated sender/queue/event/outbox race checks; no live Worker or browser."""
from __future__ import annotations
import contextlib,io,json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from RemoteAgent import local_telegram_sender as sender
from agent_core.paths import remote_tasks_path,remote_events_path
from agent_core.task_state import RemoteTaskQueue,TaskStateStore
from agent_core.remote_events import RemoteEvent,RemoteEventStore,DeliveryManager

class SenderDeliveryRace(unittest.TestCase):
    def test_queue_completion_waits_for_delayed_event_and_real_outbox(self):
        with tempfile.TemporaryDirectory(prefix='2a-sender-race-') as tmp:
            root=Path(tmp);workspace=root/'workspace';workspace.mkdir()
            original_poll=sender.TelegramReceiver.poll_once
            original_retry=DeliveryManager.retry_ready
            task=None;calls=0
            def completed_before_event(receiver):
                nonlocal task
                accepted=original_poll(receiver)
                self.assertEqual(len(accepted),1)
                queue=RemoteTaskQueue(TaskStateStore(remote_tasks_path(root)))
                task=queue.complete(accepted[0].task_id,result={'response':'isolated terminal'})
                return accepted
            def delayed_retry(manager,*args,**kwargs):
                nonlocal calls
                calls+=1
                if calls==3:
                    manager.store.emit('TASK_COMPLETED',task,status='COMPLETED',payload={'response':'isolated terminal'})
                return original_retry(manager,*args,**kwargs)
            sink={}
            with patch.object(sender,'_supervisor_ready',return_value=(True,'isolated')), \
                 patch.object(sender.TelegramReceiver,'poll_once',completed_before_event), \
                 patch.object(DeliveryManager,'retry_ready',delayed_retry), \
                 patch.object(sender.PIT,'begin'),patch.object(sender.PIT,'record'), \
                 patch.dict('os.environ',{'TRI_ONE_TASK_RESULT_V1':'0'}), \
                 contextlib.redirect_stdout(io.StringIO()):
                code=sender.send_and_wait(request='isolated text',workspace=str(workspace),timeout_sec=8,root=root,result_sink=sink)
            self.assertEqual(code,0)
            self.assertGreaterEqual(calls,3,'must not return at queue.complete before event emission')
            store=RemoteEventStore(remote_events_path(root))
            terminals=[e for e in store.events.values() if e.event_type=='TASK_COMPLETED']
            self.assertEqual(len(terminals),1)
            self.assertEqual(terminals[0].delivery_state,'DELIVERED')
            self.assertEqual(terminals[0].attempts,1)
            lines=[json.loads(x) for x in Path(terminals[0].reply_route['outbox_path']).read_text(encoding='utf-8').splitlines()]
            self.assertEqual(sum(x['event']['event_id']==terminals[0].event_id for x in lines),1)
            self.assertEqual(sink['delivery_status'],'DELIVERED')

    def test_missing_delivery_ack_times_out_instead_of_success(self):
        with tempfile.TemporaryDirectory(prefix='2a-sender-no-ack-') as tmp:
            root=Path(tmp);workspace=root/'workspace';workspace.mkdir()
            original_poll=sender.TelegramReceiver.poll_once
            def complete_with_event(receiver):
                accepted=original_poll(receiver)
                queue=RemoteTaskQueue(TaskStateStore(remote_tasks_path(root)))
                task=queue.complete(accepted[0].task_id,result={'response':'isolated terminal'})
                RemoteEventStore(remote_events_path(root)).emit('TASK_COMPLETED',task,status='COMPLETED')
                return accepted
            import time
            from RemoteAgent.local_test_delivery import LocalTestDeliveryAdapter
            clock=iter(range(100))
            fake_time=SimpleNamespace(time=time.time,monotonic=lambda:next(clock),sleep=lambda _:None)
            sink={}
            original_deliver=LocalTestDeliveryAdapter.deliver_event
            def reject_terminal(adapter,route,event):
                if event['event_type']=='TASK_COMPLETED':return {'delivered':False,'reason':'isolated_transport_unavailable'}
                return original_deliver(adapter,route,event)
            with patch.object(sender,'_supervisor_ready',return_value=(True,'isolated')), \
                 patch.object(sender.TelegramReceiver,'poll_once',complete_with_event), \
                 patch.object(LocalTestDeliveryAdapter,'deliver_event',reject_terminal), \
                 patch.object(sender,'time',fake_time), \
                 patch.object(sender.PIT,'begin'),patch.object(sender.PIT,'record'), \
                 patch.dict('os.environ',{'TRI_ONE_TASK_RESULT_V1':'0'}), \
                 contextlib.redirect_stdout(io.StringIO()):
                code=sender.send_and_wait(request='isolated delivery failure',workspace=str(workspace),timeout_sec=5,root=root,result_sink=sink)
            self.assertEqual(code,2)
            self.assertEqual(sink['observed_task_state'],'COMPLETED')
            self.assertEqual(sink['delivery_status'],'PENDING')
            self.assertEqual(sink['terminal_state'],'')

    def test_foreign_or_undelivered_terminal_cannot_confirm_delivery(self):
        with tempfile.TemporaryDirectory(prefix='2a-sender-identity-') as tmp:
            store=RemoteEventStore(Path(tmp)/'events.json')
            task=SimpleNamespace(task_id='TASK-TEST',request_id='REQ-TEST',state='COMPLETED')
            event=RemoteEvent('EVT-TEST','TASK_COMPLETED',task.request_id,task.task_id,'COMPLETED',{'transport':'LOCAL_TEST'},{})
            store.events[event.event_id]=event;store.save()
            self.assertIsNone(sender._delivered_terminal_event(store,task))
            event.delivery_state='DELIVERED';event.request_id='REQ-FOREIGN';store.events[event.event_id]=event;store.save()
            self.assertIsNone(sender._delivered_terminal_event(store,task))
            event.request_id=task.request_id;event.reply_route={'transport':'TELEGRAM'};store.events[event.event_id]=event;store.save()
            self.assertIsNone(sender._delivered_terminal_event(store,task))
            event.reply_route={'transport':'LOCAL_TEST'};event.event_type='TASK_FAILED';store.events[event.event_id]=event;store.save()
            self.assertIsNone(sender._delivered_terminal_event(store,task))
            event.event_type='TASK_COMPLETED';store.events[event.event_id]=event;store.save()
            self.assertIsNotNone(sender._delivered_terminal_event(store,task))

if __name__=='__main__': unittest.main()

