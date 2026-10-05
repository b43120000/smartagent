#!/usr/bin/env python3
from __future__ import annotations
import argparse,json,os,subprocess,sys,time
from pathlib import Path
from agent_core.process_file_lock import _pid_alive
from agent_core.runtime_cleanup import RuntimeCleanupManager
from agent_core.workspace import AGENT_PROJECT_ROOT
from agent_core.paths import remote_restart_log_path, remote_supervisor_state_path, telegram_listener_state_path
from RemoteAgent.local_telegram_config import default_store

RECONNECT_COMMAND="重啟連線"
RECONNECTED_MESSAGE="已重新連線"
RESTART_LOG_NAME="remote_restart.txt"

def _log(root,message):
    try:
        path=remote_restart_log_path(root)
        path.parent.mkdir(parents=True,exist_ok=True)
        with path.open("a",encoding="utf-8") as handle:
            handle.write(f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] {message}\n")
    except OSError:
        pass

def spawn_restart_helper(*,root,old_pid,old_generation,chat_id,reply_to_message_id,popen=subprocess.Popen):
    cmd=[sys.executable,"-m","RemoteAgent.remote_restart","--root",str(Path(root).resolve()),"--old-pid",str(int(old_pid)),"--old-generation",str(old_generation or ""),"--chat-id",str(int(chat_id)),"--reply-to-message-id",str(int(reply_to_message_id))]
    flags=0
    if os.name=="nt": flags=(getattr(subprocess,"CREATE_NEW_PROCESS_GROUP",0)|getattr(subprocess,"DETACHED_PROCESS",0)|getattr(subprocess,"CREATE_BREAKAWAY_FROM_JOB",0x01000000))
    proc=popen(cmd,cwd=str(Path(root).resolve()),creationflags=flags,close_fds=(os.name!="nt"))
    return int(proc.pid)

def _load(path):
    try:
        value=json.loads(Path(path).read_text(encoding="utf-8"));return value if isinstance(value,dict) else {}
    except Exception:return {}

def wait_for_old_exit(pid,timeout_sec=60.0):
    deadline=time.monotonic()+timeout_sec
    while _pid_alive(int(pid)):
        if time.monotonic()>=deadline: raise TimeoutError(f"remote_restart_old_process_timeout:{pid}")
        time.sleep(0.2)

def force_stop_all_agents(root,timeout_sec=120.0,exclude_pid=None):
    root=Path(root).resolve();stopper=root/"force_stop_all_agents.bat"
    if not stopper.is_file(): raise FileNotFoundError(str(stopper))
    excluded=os.getpid() if exclude_pid is None else int(exclude_pid)
    completed=subprocess.run(
        ["cmd.exe","/d","/c",str(stopper),"--no-pause","--exclude-pid",str(excluded)],
        cwd=str(root),capture_output=True,text=True,encoding="utf-8",
        errors="replace",timeout=timeout_sec,check=False,
        creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0) if os.name=="nt" else 0,
    )
    if completed.returncode:
        detail=str(completed.stderr or completed.stdout or "")[-2000:]
        raise RuntimeError(f"force_stop_all_agents_failed:{completed.returncode}:{detail}")
    return completed

def launch_remote_agent(root):
    root=Path(root).resolve();launcher=root/"launch_remote_agent.bat"
    if not launcher.is_file(): raise FileNotFoundError(str(launcher))
    flags=getattr(subprocess,"CREATE_NEW_CONSOLE",0) if os.name=="nt" else 0
    return subprocess.Popen(["cmd.exe","/d","/c",str(launcher)],cwd=str(root),creationflags=flags)

def wait_until_reconnected(root,old_generation,timeout_sec=120.0):
    root=Path(root).resolve();manager=RuntimeCleanupManager(root,"remote");deadline=time.monotonic()+timeout_sec
    while time.monotonic()<deadline:
        state=manager.read_state();listener=_load(telegram_listener_state_path(root));supervisor=_load(remote_supervisor_state_path(root))
        generation=str(state.get("generation_id","") or "")
        ready=bool(generation and generation!=str(old_generation or "") and str(state.get("status","")).upper()=="READY" and str(state.get("waiting_state",""))=="WAITING_SIGNAL")
        listener_ready=str(listener.get("status","")).upper()=="RUNNING" and _pid_alive(int(listener.get("pid",0) or 0))
        supervisor_ready=str(supervisor.get("status","")).upper()=="RUNNING" and _pid_alive(int(supervisor.get("pid",0) or 0))
        if ready and listener_ready and supervisor_ready:return generation
        time.sleep(0.25)
    raise TimeoutError("remote_restart_ready_timeout")

def send_result(root,chat_id,reply_id,text):
    from RemoteAgent.telegram_transport import TelegramBotClient,TelegramReceiverConfig
    config=default_store(root).load();client=TelegramBotClient(TelegramReceiverConfig(bot_token=config["bot_token"]))
    client.send_message(int(chat_id),str(text),reply_to_message_id=int(reply_id) or None)

def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument("--root",default=str(AGENT_PROJECT_ROOT));p.add_argument("--old-pid",type=int,required=True);p.add_argument("--old-generation",default="");p.add_argument("--chat-id",type=int,required=True);p.add_argument("--reply-to-message-id",type=int,default=0);a=p.parse_args(argv);root=Path(a.root).resolve()
    try:
        _log(root,f"RESTART_HELPER_START pid={os.getpid()} old_pid={a.old_pid} old_generation={a.old_generation}")
        # The old host exits first so this detached helper is no longer part of
        # the process tree that force_stop_all_agents.bat is about to remove.
        wait_for_old_exit(a.old_pid)
        _log(root,"OLD_PROCESS_EXITED")
        force_stop_all_agents(root)
        _log(root,"FORCE_STOP_DONE")
        launch_remote_agent(root)
        _log(root,"LAUNCH_STARTED")
        wait_until_reconnected(root,a.old_generation)
        _log(root,"NEW_RUNTIME_READY")
        send_result(root,a.chat_id,a.reply_to_message_id,RECONNECTED_MESSAGE)
        return 0
    except Exception as exc:
        _log(root,f"RESTART_FAILURE type={type(exc).__name__} message={exc}")
        try:send_result(root,a.chat_id,a.reply_to_message_id,f"重新連線失敗：{type(exc).__name__}: {exc}")
        except Exception:pass
        return 1
if __name__=="__main__":raise SystemExit(main())
