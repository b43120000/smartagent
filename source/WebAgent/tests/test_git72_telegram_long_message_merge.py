"""Git72 Telegram Listener SW-only long-text merge UT/IT.

No network access, Telegram Bot, Planner, tools or security policy changes.
These tests exercise the actual TelegramReceiver to Gateway boundary and
persisted Telegram offset; fixtures are isolated under tmp_path.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from RemoteAgent.telegram_long_message_merge import (
    MERGE_SCHEMA, TelegramLongMessageCoalescer,
)
from RemoteAgent.telegram_transport import (
    TelegramOffsetStore, TelegramReceiver, TelegramReceiverConfig,
)


class Client:
    def __init__(self, batches=()):
        self.messages = []
        self.updates = list(batches)
        self.polled = []

    def send_message(self, chat_id, text, **kw):
        self.messages.append((chat_id,text,kw))
        return {"ok":True}

    def get_updates(self, *, offset, timeout):
        self.polled.append((offset,timeout))
        return self.updates.pop(0) if self.updates else []


class Ingress:
    def __init__(self):
        self.messages = []
        self.tasks = {}

    def accept(self, message, *, workspace):
        self.messages.append(message)
        key=message.idempotency_key
        created=key not in self.tasks
        if created:
            task=SimpleNamespace(
                task_id="TASK-SW-"+str(len(self.tasks)+1),
                request_id="RR-SW-"+str(len(self.tasks)+1),
            )
            self.tasks[key]=task
        else:
            task=self.tasks[key]
        return SimpleNamespace(task=task,created=created,response="")


def update(id, text, *, chat=200, user=100, msgid=None, date=1750000000,
           reply=False, photo=False):
    m={"message_id":id if msgid is None else msgid,
       "date":date,
       "chat":{"id":chat,"type":"private"},
       "from":{"id":user,"is_bot":False},
       "text":text}
    if reply:
        m["reply_to_message"]={"message_id":10}
    if photo:
        m["photo"]=[{"file_id":"fakefile","file_size":1}]
    return {"update_id":id,"message":m}


def receiver(tmp_path, *, window=2.5, ingress=None, client=None):
    tmp_path.mkdir(parents=True,exist_ok=True)
    config=TelegramReceiverConfig(
        enabled=False,
        allowed_user_ids=(100,101),allowed_chat_ids=(200,201),
        workspace=str(tmp_path),poll_timeout_sec=25,
        long_message_merge_enabled=True,
        long_message_merge_window_sec=window,
        long_message_merge_threshold_chars=5000,
        long_message_merge_min_part_chars=900,
    )
    ingress=ingress or Ingress()
    client=client or Client()
    log=[]
    rx=TelegramReceiver(
        config=config,client=client,
        offset_store=TelegramOffsetStore(tmp_path/"telegram_offset.json"),
        ingress=ingress,install_root=tmp_path,
    )
    rx._log=lambda event,**kw:log.append((event,kw))
    rx._callback_control=lambda update:(None,"","")
    rx._process_pre_resume_choice=lambda *args,**kwargs:False
    rx._offer_pre_resume_decision=lambda *args,**kwargs:False
    return rx,ingress,client,log


def force_quiet(rx):
    assert rx._long_merge is not None
    for group in rx._long_merge.groups.values():
        group["last_arrival"]-=20
    rx._long_merge._write()
    return rx._process_updates([])


def test_two_telegram_messages_over_5000_create_exactly_one_task(tmp_path):
    rx,ingress,_,logs=receiver(tmp_path)
    first="P0_"+("甲"*3900)+"C:\\\\smart"
    second="agent"+"乙"*2200
    assert len(first)<4096 and len(second)<4096
    accepted=rx._process_updates([
        update(901,first,msgid=51),
        update(902,second,msgid=52,date=1750000001),
    ])
    assert accepted==[]
    assert rx.offset_store.next_offset==903
    assert not ingress.messages
    assert rx._long_merge.has_pending
    queued=force_quiet(rx)
    assert len(queued)==1 and len(ingress.tasks)==1
    msg=ingress.messages[0]
    assert msg.text==first+second
    assert len(msg.text)>5000
    assert msg.metadata["telegram_long_message_merge"]["schema"]==MERGE_SCHEMA
    assert msg.metadata["telegram_long_message_merge"]["source_update_ids"]==[901,902]
    assert msg.metadata["telegram_long_message_merge"]["source_message_ids"]==[51,52]
    assert msg.metadata["telegram_long_message_merge"]["combined_sha256"]==hashlib.sha256(
        (first+second).encode("utf-8")).hexdigest()
    assert msg.idempotency_key=="TELEGRAM|api.telegram.org|200|901"
    assert msg.reply_context["message_id"]==52
    assert not rx._long_merge.has_pending
    assert any(kw.get("stage")=="LONG_MESSAGE_INGRESS_FLUSHED"
               and kw.get("merged") for _,kw in logs)


def test_three_parts_and_exact_text_order(tmp_path):
    rx,ingress,_,_=receiver(tmp_path)
    texts=["A"*2100,"B"*2100,"C"*2100]
    rx._process_updates([update(10+i,text,msgid=80+i,date=1750000000+i)
                         for i,text in enumerate(texts)])
    tasks=force_quiet(rx)
    assert len(tasks)==1
    assert ingress.messages[0].text=="".join(texts)
    assert ingress.messages[0].metadata["telegram_long_message_merge"]["part_count"]==3


def test_near_messages_but_total_under_5000_remain_separate(tmp_path):
    rx,ingress,_,_=receiver(tmp_path)
    rx._process_updates([update(10,"a"*1200,msgid=2),
                         update(11,"b"*1200,msgid=3)])
    accepted=force_quiet(rx)
    assert len(accepted)==2
    assert len(ingress.tasks)==2
    assert [x.text for x in ingress.messages]==["a"*1200,"b"*1200]


def test_non_contiguous_message_ids_are_never_merged(tmp_path):
    rx,ingress,_,_=receiver(tmp_path)
    rx._process_updates([update(10,"a"*3600,msgid=2),
                         update(11,"b"*3600,msgid=4)])
    # First group flushed when second message 4 arrived, not merged.
    assert len(ingress.messages)==1
    force_quiet(rx)
    assert [x.text for x in ingress.messages]==["a"*3600,"b"*3600]


def test_timestamp_gap_prevents_merge_even_if_same_poll(tmp_path):
    rx,ingress,_,_=receiver(tmp_path)
    rx._process_updates([update(10,"a"*3600,msgid=2),
                         update(11,"b"*3600,msgid=3,date=1750000010)])
    force_quiet(rx)
    assert len(ingress.tasks)==2


def test_cross_sender_or_chat_does_not_mix_original_content(tmp_path):
    rx,ingress,_,_=receiver(tmp_path)
    rx._process_updates([update(10,"a"*3600,msgid=2),
                         update(11,"b"*3600,chat=201,user=101,msgid=3)])
    force_quiet(rx)
    assert len(ingress.tasks)==2
    assert {x.text for x in ingress.messages}=={"a"*3600,"b"*3600}


def test_short_controls_are_immediate_and_flush_pending_first(tmp_path):
    rx,ingress,client,_=receiver(tmp_path)
    rx._process_updates([update(10,"a"*3600,msgid=2)])
    assert not ingress.messages
    rx._process_updates([update(11,"普通指令",msgid=3)])
    assert [x.text for x in ingress.messages]==["a"*3600,"普通指令"]
    assert not rx._long_merge.has_pending
    assert rx.offset_store.next_offset==12


def test_direct_cmd_never_staged(tmp_path):
    rx,ingress,_,_=receiver(tmp_path)
    msg="執行cmd "+("a"*3500)
    assert not rx._long_message_eligible(update(10,msg))
    assert not rx._long_merge.has_pending


def test_slash_and_reply_media_never_merged(tmp_path):
    rx,_,_,_=receiver(tmp_path)
    assert not rx._long_message_eligible(update(10,"/new "+"a"*3600))
    assert not rx._long_message_eligible(update(10,"a"*3600,reply=True))
    assert not rx._long_message_eligible(update(10,"a"*3600,photo=True))
    assert not rx._long_message_eligible(update(10,"a"*3600,user=102))
    assert not rx._long_message_eligible(update(10,"a"*3600,chat=202))


def test_restart_between_parts_is_durable_and_does_not_duplicate(tmp_path):
    ingress=Ingress()
    rx,_,_,_=receiver(tmp_path,ingress=ingress)
    rx._process_updates([update(101,"甲"*3700,msgid=10)])
    assert rx.offset_store.next_offset==102
    fresh,_,_,_=receiver(tmp_path,ingress=ingress)
    assert fresh._long_merge.has_pending
    assert fresh.offset_store.next_offset==102
    fresh._process_updates([update(102,"乙"*1800,msgid=11,date=1750000001)])
    accepted=force_quiet(fresh)
    assert len(accepted)==1 and len(ingress.tasks)==1
    assert ingress.messages[0].text=="甲"*3700+"乙"*1800


def test_staged_duplicate_during_offset_write_failure_is_safe(tmp_path,monkeypatch):
    rx,ingress,_,logs=receiver(tmp_path)
    original=rx.offset_store.commit
    count=0
    def fail_once(update_id):
        nonlocal count
        count+=1
        if count==1: raise OSError("simulated offset disk failure")
        return original(update_id)
    monkeypatch.setattr(rx.offset_store,"commit",fail_once)
    msg=update(101,"A"*3900,msgid=2)
    with pytest.raises(OSError,match="offset disk failure"):
        rx._process_updates([msg])
    assert rx.offset_store.next_offset==0
    assert rx._long_merge.has_pending
    rx._process_updates([msg])
    assert rx.offset_store.next_offset==102
    assert len(rx._long_merge.groups["200:100"]["updates"])==1
    assert any(kw.get("stage")=="LONG_MESSAGE_PART_REPLAYED" for _,kw in logs)
    force_quiet(rx)
    assert len(ingress.tasks)==1


def test_failed_pending_save_does_not_advance_offset(tmp_path,monkeypatch):
    rx,_,_,_=receiver(tmp_path)
    old=rx._long_merge._write
    def disk_full():
        raise OSError("pending disk full")
    monkeypatch.setattr(rx._long_merge,"_write",disk_full)
    with pytest.raises(OSError,match="pending disk full"):
        rx._process_updates([update(101,"A"*3600)])
    assert not rx._long_merge.has_pending
    assert rx.offset_store.next_offset==0
    monkeypatch.setattr(rx._long_merge,"_write",old)


def test_transient_ingress_failure_retains_staging_for_restart(tmp_path):
    class RetryIngress(Ingress):
        fail=True
        def accept(self,message,*,workspace):
            if self.fail:
                self.fail=False
                raise RuntimeError("simulated queue failure")
            return super().accept(message,workspace=workspace)
    ingress=RetryIngress()
    rx,_,_,_=receiver(tmp_path,ingress=ingress)
    rx._process_updates([update(101,"A"*3600,msgid=10),
                         update(102,"B"*1800,msgid=11,date=1750000001)])
    rx._long_merge.groups["200:100"]["last_arrival"]-=20
    rx._long_merge._write()
    with pytest.raises(RuntimeError,match="queue failure"):
        rx._process_updates([])
    assert rx._long_merge.has_pending
    fresh,_,_,_=receiver(tmp_path,ingress=ingress)
    accepted=fresh._process_updates([])
    assert len(accepted)==1 and len(ingress.tasks)==1


def test_poll_uses_one_second_timeout_while_pending(tmp_path):
    client=Client([ [update(10,"X"*3600,msgid=2)], [] ])
    rx,_,_,_=receiver(tmp_path,client=client)
    rx.poll_once()
    assert client.polled[0][1]==25
    rx.poll_once()
    assert client.polled[1][1]==1


def test_corrupt_pending_state_fails_closed(tmp_path):
    directory=tmp_path/"localdata"/"runtime"
    directory.mkdir(parents=True)
    file=directory/"telegram_long_message_pending.json"
    file.write_text('{"schema":"BAD","groups":{}}',encoding="utf-8")
    with pytest.raises(ValueError,match="PENDING_STATE_INVALID"):
        receiver(tmp_path)



def test_actual_gateway_task_store_one_task_and_stable_request_id(tmp_path, monkeypatch):
    """Actual TelegramReceiver → AgentIngressGateway → durable TaskStateStore."""
    from agent_core.agent_gateway import AgentIngressGateway
    from agent_core.task_state import RemoteTaskQueue, TaskStateStore
    from agent_core.transport_sessions import TransportSessionRouter

    workspace=tmp_path/"workspace"
    workspace.mkdir()
    monkeypatch.setattr(
        "agent_core.task_workspace_resolver._policy_roots",
        lambda: ((workspace.resolve(),), ()),
    )
    store=TaskStateStore(tmp_path/"actual_tasks.json")
    queue=RemoteTaskQueue(store)
    gateway=AgentIngressGateway(
        task_queue=queue,
        session_router=TransportSessionRouter(tmp_path/"actual_sessions.json"),
    )
    rx,_,_,_=receiver(workspace,ingress=gateway)
    first="請執行完整測試：\n"+("甲"*3950)
    second="\n繼續P2～P9："+("乙"*1300)
    assert len(first)<4096 and len(second)<4096
    assert rx._process_updates([
        update(912,first,msgid=20),
        update(913,second,msgid=21,date=1750000001),
    ])==[]
    results=force_quiet(rx)
    assert len(results)==1
    task=results[0]
    assert task.request==first+second
    assert task.request_id.startswith("RR-TELEGRAM-")
    assert task.origin_turn_fingerprint=="TELEGRAM|api.telegram.org|200|912"
    assert task.metadata["ingress_metadata"]["telegram_long_message_merge"][
        "source_update_ids"]==[912,913]
    assert task.metadata["source_user_command"]==first+second
    store_reloaded=TaskStateStore(store.path)
    store_reloaded.load()
    assert len(store_reloaded.all())==1
    assert store_reloaded.get(task.task_id).request==first+second
    assert len(queue.queued())==1


def test_replay_after_gateway_accept_before_pending_clear_has_one_task(tmp_path,monkeypatch):
    from agent_core.agent_gateway import AgentIngressGateway
    from agent_core.task_state import RemoteTaskQueue, TaskStateStore
    from agent_core.transport_sessions import TransportSessionRouter

    workspace=tmp_path/"workspace"
    workspace.mkdir()
    monkeypatch.setattr(
        "agent_core.task_workspace_resolver._policy_roots",
        lambda: ((workspace.resolve(),), ()),
    )
    store=TaskStateStore(tmp_path/"actual_tasks.json")
    queue=RemoteTaskQueue(store)
    gateway=AgentIngressGateway(
        task_queue=queue,
        session_router=TransportSessionRouter(tmp_path/"actual_sessions.json"),
    )
    rx,_,_,_=receiver(workspace,ingress=gateway)
    rx._process_updates([update(91,"A"*3900,msgid=21),
                         update(92,"B"*1400,msgid=22,date=1750000001)])
    group=rx._long_merge.groups["200:100"]
    group["last_arrival"]-=20
    rx._long_merge._write()
    old=rx._long_merge.clear
    def fail_clear(_):
        raise OSError("simulated pending clear failure")
    monkeypatch.setattr(rx._long_merge,"clear",fail_clear)
    with pytest.raises(OSError,match="pending clear failure"):
        rx._process_updates([])
    assert len(queue.queued())==1
    # New receiver must re-submit the SAME idempotency key and not create
    # a second durable task despite the crash after the first accept.
    fresh,_,_,_=receiver(workspace,ingress=gateway)
    tasks=fresh._process_updates([])
    assert len(tasks)==1
    assert len(queue.queued())==1
    assert tasks[0].request_id==queue.queued()[0].request_id
