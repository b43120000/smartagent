#!/usr/bin/env python3
from __future__ import annotations
import json,os,re,time,threading
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from playwright.sync_api import sync_playwright
from agent_core.conversation_registry import ConversationRegistry
from agent_core.task_state import RemoteTaskQueue,TaskStateStore
from agent_core.workspace import AGENT_PROJECT_ROOT
from agent_core.paths import (agent_host_state_path, install_root, protocol_manifest_path, remote_events_path, remote_runtime_log_path, remote_runtime_state_path, remote_tasks_path, remote_transport_sessions_path, source_root, telegram_offset_path, telegram_pairing_path)
from RemoteAgent.remote_ingress import RemoteIngressObserver,CURSOR_NAME
from RemoteAgent.remote_scheduler import RemoteWatchScheduler
from RemoteAgent.webgpt_transport import WebGPTTransportAdapter,CONNECTED
from RemoteAgent.webgpt_delivery import WebGPTDeliveryAdapter
from RemoteAgent.remote_worker_launcher import RemoteWorkerLauncher
from RemoteAgent.telegram_delivery import TelegramDeliveryAdapter
from RemoteAgent.telegram_transport import (
    TelegramBotClient,
    TelegramOffsetStore,
    TelegramReceiver,
    TelegramReceiverConfig,
)
from RemoteAgent.telegram_pairing import TelegramPairingStore
from RemoteAgent.local_test_delivery import LocalTestDeliveryAdapter
from RemoteAgent.transport_ingress import TransportIngressAdapter
from agent_core.remote_events import RemoteEventStore,DeliveryManager
from agent_core.remote_runtime_log import RemoteRuntimeLog
from agent_core.webgpt_rate_governor import WebGPTRateGovernor
from agent_core.conversation_identity import conversation_id
from agent_core.transport_sessions import TransportSessionRouter
from agent_core.agent_gateway import AgentIngressGateway
from agent_core.webgpt_outbound_ledger import record_outbound_turn
from agent_core.web_ui import create_web_ui_for_page, provider_from_url
from WebAgent.browser_bridge import REMOTE_AGENT_EXECUTION_PAGE_MARKER,execution_page_lease

def _enable_runtime_console()->bool:
    """Allocate a console owned by the real runtime process for development."""
    if os.name!='nt' or os.environ.get('SMARTAGENT_REMOTE_CONSOLE','0')!='1': return False
    try:
        import ctypes
        kernel32=ctypes.windll.kernel32
        # A standalone RemoteAgent inherits the launch_remote_agent.bat console.
        # The legacy combined launcher creates a new console for Agent0.  Treat
        # either case as a visible development console so runtime events are
        # mirrored consistently without opening an extra CMD window.
        if not kernel32.GetConsoleWindow() and not kernel32.AllocConsole():
            return False
        kernel32.SetConsoleOutputCP(65001)
        kernel32.SetConsoleTitleW('SmartAgent RemoteAgent-0 Runtime')
        sys.stdout=open('CONOUT$','w',encoding='utf-8',buffering=1)
        sys.stderr=open('CONOUT$','w',encoding='utf-8',buffering=1)
        print('[RemoteAgent-0] Development console attached. Waiting for linked conversation signals.',flush=True)
        return True
    except Exception:
        return False

class BrowserUIScraper:
    def __init__(self,page,rate_governor=None,yield_requested=None):
        self._page=page
        self._web_ui=None
        self._web_ui_provider=""
        self._rate_governor=rate_governor or WebGPTRateGovernor(ROOT)
        self._yield_requested=yield_requested
    def _web_ui_adapter(self):
        provider=provider_from_url(str(getattr(self._page,'url','') or ''))
        if self._web_ui is None or self._web_ui_provider!=provider:
            self._web_ui=create_web_ui_for_page(self._page,provider=provider)
            self._web_ui_provider=provider
        return self._web_ui
    def navigate_to_conversation(self,url):
        from agent_core.remote_binding import active
        binding = active()
        if binding and conversation_id(url) != conversation_id(binding['gpt_url']):
            raise RuntimeError('remote_navigation_binding_mismatch')
        current=str(getattr(self._page,'url','') or '')
        # page.goto() can wait indefinitely on ChatGPT's project redirect and
        # leave the dedicated Agent0 target unresponsive. Assign the route and
        # observe URL identity instead; this never touches another page.
        if RemoteAgentRuntime._conversation_identity(current)!=RemoteAgentRuntime._conversation_identity(url):
            self._page.evaluate("target => { window.location.assign(target); }",str(url))
        deadline=time.monotonic()+10.0
        while time.monotonic()<deadline:
            current=str(getattr(self._page,'url','') or '')
            if RemoteAgentRuntime._conversation_identity(current)==RemoteAgentRuntime._conversation_identity(url):
                try:
                    composer=self._web_ui_adapter().visible_composer()
                    if composer is not None and composer.is_visible():
                        return
                except Exception:
                    pass
            self._page.wait_for_timeout(200)
        raise RuntimeError('conversation_navigation_not_ready')
    def ask(self,prompt:str,new_conversation:bool=False)->str:
        lease=self._rate_governor.acquire(wait=True,conversation_key=conversation_id(str(getattr(self._page,'url','') or '')))
        try:
            return self._ask_with_rate_lease(prompt,new_conversation=new_conversation,rate_lease=lease)
        finally:
            lease.release()
    def _ask_with_rate_lease(self,prompt:str,new_conversation:bool,rate_lease)->str:
        """Deliver one event through the existing conversation composer.

        Agent0 keeps the cross-process submit lease until ChatGPT finishes the
        event answer.  Its dedicated heartbeat thread remains independent while
        this method waits. Observing a new user turn still proves the event
        crossed the composer boundary and prevents duplicate submissions.
        """
        if new_conversation:
            raise RuntimeError('agent0_new_conversation_forbidden')
        text=str(prompt or '').strip()
        if not text:
            raise ValueError('empty_delivery_prompt')
        page=self._page
        web_ui=self._web_ui_adapter()
        scope=web_ui.capture_request(text,conversation_id=conversation_id(str(getattr(page,'url','') or '')))
        composer=web_ui.visible_composer()
        if composer is None:
            raise RuntimeError('web_ui_composer_unavailable')
        composer.wait_for(state='visible',timeout=20000)
        composer.fill(text)
        send=web_ui.send_control()
        if send is None:
            raise RuntimeError('web_ui_send_control_unavailable')
        send.wait_for(state='visible',timeout=10000)
        rate_lease.before_submit()
        try:
            send.click(timeout=10000)
        finally:
            rate_lease.record_submit()
        deadline=time.monotonic()+10.0
        while time.monotonic()<deadline:
            if web_ui.confirm_user_turn(scope) is not None:
                record_outbound_turn(str(getattr(page,'url','') or ''),scope.baseline_user_count,text)
                self._wait_for_answer_idle(scope)
                return 'REMOTE_AGENT_EVENT_SENT'
            page.wait_for_timeout(100)
        raise RuntimeError('webgpt_delivery_not_acknowledged')
    def _wait_for_answer_idle(self,scope)->bool:
        """Keep the global lease until the event's ChatGPT answer is complete."""
        deadline=time.monotonic()+max(10.0,float(os.environ.get('SMARTAGENT_REMOTE_EVENT_ANSWER_TIMEOUT_SEC','120')))
        stable=''; stable_since=0.0
        while time.monotonic()<deadline:
            if callable(self._yield_requested) and self._yield_requested():
                # The event's user turn already crossed the composer boundary.
                # Its generated acknowledgement is expendable; stop it so a
                # newly queued execution request can own the shared page.
                for stop in self._web_ui_adapter().stop_controls():
                    try:
                        stop.click(timeout=2000)
                        break
                    except Exception:
                        continue
                settle_deadline=time.monotonic()+5.0
                while time.monotonic()<settle_deadline:
                    try:
                        if not self._web_ui_adapter().generation_active():
                            break
                    except Exception:
                        break
                    self._page.wait_for_timeout(100)
                return False
            web_ui=self._web_ui_adapter()
            assistant=web_ui.latest_owned_assistant(scope)
            activity=web_ui.activity(assistant)
            # Static media is response content, not proof that generation is
            # still running. Only provider-owned generation/busy signals hold
            # the execution-page lease open.
            active=bool(activity.generating or activity.busy_count)
            if assistant is not None:
                current=web_ui.extract_final_text(assistant)
                if current!=stable:
                    stable=current; stable_since=time.monotonic()
                elif current and not active and time.monotonic()-stable_since>=2.0:
                    return True
            self._page.wait_for_timeout(250)
        return False

class RemoteAgentRuntime:
    PAGE_OWNER_MARKER=REMOTE_AGENT_EXECUTION_PAGE_MARKER
    def __init__(self,*,cdp:str,poll:float=2.0,parent_pid:int=0):
        console_enabled=_enable_runtime_console()
        self.cdp=cdp; self.poll=max(1.0,float(poll)); self.parent_pid=int(parent_pid or 0)
        self.registry=ConversationRegistry(); self.registry.load()
        self.queue=RemoteTaskQueue(TaskStateStore(remote_tasks_path()))
        self.scheduler=RemoteWatchScheduler(self.registry)
        self.event_store=RemoteEventStore(remote_events_path())
        self.runtime_log=RemoteRuntimeLog(remote_runtime_log_path(),mirror_console=console_enabled)
        self.session_router=TransportSessionRouter(remote_transport_sessions_path())
        self.runtime_state_path=remote_runtime_state_path()
        self._state_write_lock=threading.Lock()
        self._heartbeat_stop=threading.Event()
        self._heartbeat_thread=None
        self._heartbeat_interval=max(1.0,float(os.environ.get('SMARTAGENT_REMOTE_HEARTBEAT_SEC','5')))
        self.delivery=None; self.worker_launcher=None
        self.telegram_receiver=None; self._telegram_config=None
        self._pw=None; self._browser=None; self._owned_page=None; self._owned_page_created=False; self.scraper=None; self.adapter=None
        self._next_webgpt_attach_at=0.0
        self._webgpt_attach_retry_sec=max(1.0,float(os.environ.get('SMARTAGENT_REMOTE_CDP_RETRY_SEC','2')))
        self._worker_waiting_for_browser_logged=False
        self._demand_scoped=os.environ.get('SMARTAGENT_REMOTE_DEMAND_SCOPED','0')=='1'
        self._ingress_first=os.environ.get('SMARTAGENT_REMOTE_INGRESS_FIRST','0')=='1'
        self._telegram_receiver_disabled=os.environ.get('SMARTAGENT_TELEGRAM_RECEIVER_DISABLED','0')=='1'
    def _execution_work_waiting(self)->bool:
        """Refresh cross-process ingress before browser-delivery decisions."""
        try:
            with self.queue.store.process_lock():
                self.queue.store.load()
                return bool(self.queue.queued() or self.queue.running())
        except Exception:
            # Failing closed keeps event delivery from stealing the execution
            # page when queue state cannot be established safely.
            return True
    def _write_state(self,status:str,**detail):
        payload={"version":1,"status":str(status),"runtime_pid":os.getpid(),"parent_pid":self.parent_pid,"heartbeat_at":time.time(),"cdp":self.cdp,**detail}
        lock=getattr(self,'_state_write_lock',None)
        if lock is None:
            lock=threading.Lock(); self._state_write_lock=lock
        with lock:
            tmp=None
            try:
                self.runtime_state_path.parent.mkdir(parents=True,exist_ok=True)
                tmp=self.runtime_state_path.with_name(
                    f"{self.runtime_state_path.name}.{os.getpid()}.{time.time_ns()}.tmp"
                )
                tmp.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
                for attempt in range(3):
                    try:
                        os.replace(tmp,self.runtime_state_path)
                        tmp=None
                        break
                    except PermissionError:
                        if attempt>=2: raise
                        time.sleep(0.02*(attempt+1))
            except Exception as exc:
                self.runtime_log.write("ERROR",component="runtime",stage="STATE_WRITE",error=f"{type(exc).__name__}: {exc}")
            finally:
                if tmp is not None:
                    try: tmp.unlink(missing_ok=True)
                    except Exception: pass

    def _start_heartbeat(self):
        """Keep Agent0 liveness independent from blocking CDP/navigation work."""
        stop=getattr(self,'_heartbeat_stop',None)
        if stop is None:
            stop=threading.Event(); self._heartbeat_stop=stop
        current=getattr(self,'_heartbeat_thread',None)
        if current is not None and current.is_alive(): return
        stop.clear()
        interval=float(getattr(self,'_heartbeat_interval',5.0) or 5.0)
        def beat():
            while not stop.wait(interval):
                worker_launcher=getattr(self,'worker_launcher',None)
                self._write_state(
                    'RUNNING',telegram_enabled=bool(getattr(self,'telegram_receiver',None)),
                    webgpt_enabled=bool(getattr(self,'adapter',None)),heartbeat_source='DEDICATED_THREAD',
                    worker_state_known=worker_launcher is not None,
                    active_worker_count=(
                        len(worker_launcher._processes)
                        if worker_launcher is not None else 0
                    ),
                )
        self._heartbeat_thread=threading.Thread(
            target=beat,name='remoteagent-runtime-heartbeat',daemon=True,
        )
        self._heartbeat_thread.start()

    def _stop_heartbeat(self):
        stop=getattr(self,'_heartbeat_stop',None)
        if stop is not None: stop.set()
        thread=getattr(self,'_heartbeat_thread',None)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
    @classmethod
    def _get_or_create_background_page(cls,ctx,*,preferred_urls=(),excluded_urls=()):
        """Return one canonical live page per ChatGPT conversation id.

        A linked conversation already open in this browser/profile is reused.
        Duplicate tabs for that same /c/<id> are collapsed instead of opening
        another page.  This rule is shared by LocalAgent/WebCopilot/RemoteAgent.
        """
        pages=[
            page for page in list(getattr(ctx,'pages',[]) or [])
            if not bool(getattr(page,'closed',False))
            and not (callable(getattr(page,'is_closed',None)) and page.is_closed())
        ]
        preferred=[cls._conversation_identity(url) for url in preferred_urls if str(url or '').strip()]
        preferred_set=set(preferred)

        # First priority is an already-open linked conversation, regardless of
        # which interface originally opened it.  The conversation id is the
        # ownership key; process-specific window.name markers are secondary.
        for identity in preferred:
            matches=[
                page for page in pages
                if cls._conversation_identity(str(getattr(page,'url','') or ''))==identity
            ]
            if matches:
                marker="SMARTAGENT_CANONICAL_CONVERSATION:"+identity
                def canonical_rank(page):
                    try: return 0 if str(page.evaluate("() => window.name") or "")==marker else 1
                    except Exception: return 1
                matches.sort(key=canonical_rank)
                canonical=matches[0]
                for duplicate in matches[1:]:
                    try: duplicate.close()
                    except Exception: pass
                return canonical,False

        # Reuse Agent0's existing utility page only when it is not a different
        # linked conversation.  This avoids navigating one conversation tab
        # onto another conversation and preserves the singleton invariant.
        for page in pages:
            identity=cls._conversation_identity(str(getattr(page,'url','') or ''))
            try:
                owned=page.evaluate("() => window.name") == cls.PAGE_OWNER_MARKER
            except Exception:
                owned=False
            if owned and (not identity or identity not in preferred_set):
                return page,False

        page=ctx.new_page()
        marker=(
            "SMARTAGENT_CANONICAL_CONVERSATION:"+preferred[0]
            if preferred else cls.PAGE_OWNER_MARKER
        )
        page.evaluate("marker => { window.name = marker; }",marker)
        if preferred_urls:
            try:
                BrowserUIScraper(page).navigate_to_conversation(str(list(preferred_urls)[0]))
            except BaseException:
                try: page.close()
                except Exception: pass
                raise
        return page,True

    @staticmethod
    def _conversation_identity(url:str)->str:
        return conversation_id(url)
    @classmethod
    def _collapse_enabled_aliases(cls,records:list[dict],current_url:str="")->list[dict]:
        """Poll one URL per conversation id, preferring the live/current alias."""
        chosen={}
        for record in records:
            identity=cls._conversation_identity(record.get("gpt_url",""))
            old=chosen.get(identity)
            if old is None:
                chosen[identity]=record; continue
            def score(row):
                return (
                    int(str(row.get("gpt_url",""))==str(current_url)),
                    float(row.get("updated_at",0.0) or 0.0),
                )
            if score(record)>score(old): chosen[identity]=record
        return list(chosen.values())
    def _parent_alive(self):
        if not self.parent_pid: return True
        if os.name=='nt':
            try:
                import ctypes
                from ctypes import wintypes
                kernel32=ctypes.windll.kernel32
                handle=kernel32.OpenProcess(0x1000,False,self.parent_pid)
                if not handle: return False
                try:
                    code=wintypes.DWORD()
                    return bool(kernel32.GetExitCodeProcess(handle,ctypes.byref(code))) and code.value==259
                finally:
                    kernel32.CloseHandle(handle)
            except Exception:
                return False
        try: os.kill(self.parent_pid,0); return True
        except OSError: return False
    def _configure_telegram(self,delivery_adapters:dict)->None:
        try:
            config=TelegramReceiverConfig.from_env()
        except Exception as exc:
            self._telegram_config=None
            self.telegram_receiver=None
            self.runtime_log.write(
                "ERROR",component="telegram_receiver",stage="CONFIG_PARSE",
                error=f"{type(exc).__name__}: {exc}",
            )
            return
        self._telegram_config=config
        if not config.enabled:
            self.runtime_log.write("CONNECT",component="telegram_receiver",stage="DISABLED")
            return
        try:
            config.validate()
            client=TelegramBotClient(config)
            ingress=TransportIngressAdapter(
                task_queue=self.queue,session_router=self.session_router,
                event_store=self.event_store,runtime_log=self.runtime_log,
            )
            pairing_store = TelegramPairingStore(telegram_pairing_path()) if config.pairing_enabled else None
            receiver=TelegramReceiver(
                config=config,client=client,
                offset_store=TelegramOffsetStore(telegram_offset_path()),
                ingress=ingress,runtime_log=self.runtime_log,pairing_store=pairing_store,
            )
            self.telegram_receiver=receiver
            delivery_adapters['TELEGRAM']=TelegramDeliveryAdapter(client)
        except Exception as exc:
            self.telegram_receiver=None
            self.runtime_log.write(
                "ERROR",component="telegram_receiver",stage="CONFIG",
                error=f"{type(exc).__name__}: {exc}",
            )
    def _allowed_worker_bindings(self):
        # Ingress and Agent0 are separate processes.  Always refresh before
        # deriving request-scoped LOCAL_TEST bindings; otherwise a request
        # accepted after Agent0 startup is filtered out until restart.
        with self.queue.store.process_lock():
            self.queue.store.load()
        bindings=[
            (row['workspace'],row['gpt_url'])
            for row in self.registry.list_remote_conversations(enabled_only=True)
        ]
        bindings.extend(
            (row['workspace'],row['gpt_url'])
            for row in self.registry.list_bindings()
            if row.get('workspace') and row.get('gpt_url')
        )
        if self.telegram_receiver is not None:
            bindings.extend(self.telegram_receiver.allowed_bindings())
        # The local Telegram simulation uses a request-scoped conversation key
        # and a filesystem reply sink.  It is authorized only after the local
        # sender has durably created the task inside this project.
        for task in self.queue.queued() + self.queue.running():
            if str((task.reply_route or {}).get('transport','')).upper() == 'LOCAL_TEST':
                bindings.append((task.workspace,task.conversation_url))
        return list(dict.fromkeys(bindings))
    def _start_telegram_ingress(self)->bool:
        if self.telegram_receiver is None:
            return False
        if bool(getattr(self,'_telegram_receiver_disabled',False)):
            self.runtime_log.write(
                'CONNECT',component='telegram_receiver',stage='DELEGATED_TO_HOST_LISTENER'
            )
            return False
        self.telegram_receiver.start()
        return True
    def _browser_context(self):
        """Return the live default CDP context, never a stale Browser handle."""
        browser=getattr(self,'_browser',None)
        if browser is None:
            return None
        try:
            is_connected=getattr(browser,'is_connected',None)
            if callable(is_connected) and not is_connected():
                return None
            contexts=list(browser.contexts or [])
            return contexts[0] if contexts else None
        except Exception:
            return None
    def _discard_stale_browser(self,reason='')->None:
        """Forget a disconnected CDP attachment without touching the host browser."""
        self.runtime_log.write(
            'RECONNECT',component='runtime',stage='STALE_BROWSER_DISCARDED',
            reason=str(reason or ''),cdp=self.cdp,
        )
        self._close_owned_page()
        pw=getattr(self,'_pw',None)
        self._pw=None; self._browser=None; self.scraper=None; self.adapter=None
        if pw is not None:
            try: pw.stop()
            except Exception: pass
    def _worker_auth_state(self):
        """Export a short-lived authenticated snapshot for an isolated Agent1."""
        self.runtime_log.write('CONNECT',component='worker_launcher',stage='BROWSER_STATE_EXPORT_START')
        try:
            ctx=self._browser_context()
            if ctx is None:
                self.runtime_log.write(
                    'RECONNECT',component='worker_launcher',
                    stage='BROWSER_STATE_EXPORT_REATTACH',cdp=self.cdp,
                )
                self._attach_local_browser_when_ready(force=True)
                ctx=self._browser_context()
            if ctx is None:
                raise RuntimeError('worker_browser_auth_context_unavailable')
            state=ctx.storage_state(indexed_db=True)
            self.runtime_log.write(
                'CONNECT',component='worker_launcher',stage='BROWSER_STATE_EXPORT_READY',
                cookie_count=len(state.get('cookies',[]) or []),origin_count=len(state.get('origins',[]) or []),
            )
            return state
        except Exception as exc:
            self.runtime_log.write(
                'ERROR',component='worker_launcher',stage='BROWSER_STATE_EXPORT_FAILED',
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
    def _connect_webgpt(self,enabled,delivery_adapters:dict)->bool:
        from agent_core.remote_binding import active
        binding = active()
        if binding:
            enabled = [binding]
        self._pw=sync_playwright().start()
        self.runtime_log.write("CONNECT",component="runtime",stage="PLAYWRIGHT_READY",cdp=self.cdp)
        self._browser=self._pw.chromium.connect_over_cdp(self.cdp,timeout=10000)
        self.runtime_log.write("CONNECT",component="runtime",stage="CDP_ATTACHED",cdp=self.cdp)
        if not self._browser.contexts: raise RuntimeError('no_browser_context')
        ctx=self._browser.contexts[0]
        foreground_urls=[]
        host_state_path=agent_host_state_path()
        try:
            host_state=json.loads(host_state_path.read_text(encoding='utf-8'))
            foreground_url=str(host_state.get('conversation_url','') or '').strip()
            if foreground_url: foreground_urls.append(foreground_url)
        except Exception:
            pass
        # Acquire the canonical linked conversation page. The /c/<id> identity
        # wins over process/interface ownership, so an existing LocalAgent or
        # WebCopilot page is reused and duplicates are collapsed.
        self.runtime_log.write("CONNECT",component="runtime",stage="PAGE_ACQUIRE",page_count=len(ctx.pages),preferred_count=len(enabled),excluded_foreground_count=len(foreground_urls))
        with execution_page_lease(timeout_sec=120.0,label="RemoteAgent common page startup"):
            page,created=self._get_or_create_background_page(
                ctx,
                preferred_urls=[row.get('gpt_url','') for row in enabled],
                excluded_urls=foreground_urls,
            )
            page.evaluate("marker => { window.name = marker; }",self.PAGE_OWNER_MARKER)
            self.runtime_log.write("CONNECT",component="runtime",stage="PAGE_ACQUIRED",page_created=created,page_url=str(getattr(page,'url','') or ''))
            self._owned_page=page; self._owned_page_created=bool(created)
            self.scraper=BrowserUIScraper(page,yield_requested=self._execution_work_waiting); self.adapter=WebGPTTransportAdapter(self.scraper)
            self._initialize_webgpt_page(enabled)
        adapter=WebGPTDeliveryAdapter(self.adapter)
        delivery_adapters['WEBGPT']=adapter
        delivery_adapters['WEBGPT_COPILOT']=adapter
        return bool(created)
    def _initialize_webgpt_page(self,enabled)->bool:
        """Move only Agent0's owned page off about:blank during startup."""
        rows=list(enabled or [])
        from agent_core.remote_binding import active
        binding = active()
        if binding:
            rows = [binding]
        if not rows or self.scraper is None: return False
        telegram_workspace=''
        if self.telegram_receiver is not None:
            try:
                bindings=self.telegram_receiver.allowed_bindings()
                if bindings: telegram_workspace=os.path.normcase(os.path.normpath(str(bindings[0][0])))
            except Exception:
                pass
        preferred=[
            row for row in rows
            if telegram_workspace and os.path.normcase(os.path.normpath(str(row.get('workspace',''))))==telegram_workspace
        ] or rows
        record=max(preferred,key=lambda row:float(row.get('updated_at',0.0) or 0.0))
        target=str(record.get('gpt_url','') or '').strip()
        if not target: return False
        try:
            self.runtime_log.write('CONNECT',component='runtime',stage='PAGE_INITIAL_NAVIGATION',conversation_url=target)
            self.scraper.navigate_to_conversation(target)
            self.runtime_log.write('CONNECT',component='runtime',stage='PAGE_INITIAL_READY',conversation_url=target)
            return True
        except Exception as exc:
            # Telegram ingress remains usable even if WebGPT is temporarily not
            # ready; the normal scheduler will reconcile this owned page later.
            self.runtime_log.write(
                'ERROR',component='runtime',stage='PAGE_INITIAL_NAVIGATION',
                conversation_url=target,error=f"{type(exc).__name__}: {exc}",
            )
            return False
    @staticmethod
    def _closed_page_error(exc)->bool:
        return type(exc).__name__=='TargetClosedError' or 'has been closed' in str(exc).lower()
    def _recover_webgpt_page(self,enabled,reason='')->bool:
        try:
            if self._browser is None or not self._browser.contexts: return False
            ctx=self._browser.contexts[0]
            foreground_urls=[]
            host_state_path=agent_host_state_path()
            try:
                host_state=json.loads(host_state_path.read_text(encoding='utf-8'))
                foreground_url=str(host_state.get('conversation_url','') or '').strip()
                if foreground_url: foreground_urls.append(foreground_url)
            except Exception:
                pass
            with execution_page_lease(timeout_sec=120.0,label="RemoteAgent common page recovery"):
                page,created=self._get_or_create_background_page(
                    ctx,preferred_urls=[row.get('gpt_url','') for row in enabled],excluded_urls=foreground_urls
                )
                page.evaluate("marker => { window.name = marker; }",self.PAGE_OWNER_MARKER)
                self._owned_page=page; self._owned_page_created=bool(created)
                self.scraper=BrowserUIScraper(page,yield_requested=self._execution_work_waiting); self.adapter=WebGPTTransportAdapter(self.scraper)
            adapter=WebGPTDeliveryAdapter(self.adapter)
            if self.delivery is not None:
                self.delivery.adapters['WEBGPT']=adapter
                self.delivery.adapters['WEBGPT_COPILOT']=adapter
            self.runtime_log.write('RECONNECT',component='runtime',stage='WEBGPT_PAGE_RECOVERED',reason=str(reason),page_created=bool(created))
            return True
        except Exception as exc:
            self.runtime_log.write('ERROR',component='runtime',stage='WEBGPT_PAGE_RECOVERY',error=f"{type(exc).__name__}: {exc}")
            return False
    def connect(self):
        from agent_core.protocol_manifest import require_protocol_manifest
        require_protocol_manifest(install_root())
        self.runtime_log.write("CONNECT",component="runtime",stage="ATTEMPT",cdp=self.cdp)
        delivery_adapters={'LOCAL_TEST':LocalTestDeliveryAdapter(ROOT)}
        self._configure_telegram(delivery_adapters)
        enabled=self._collapse_enabled_aliases(
            self.registry.list_remote_conversations(enabled_only=True)
        )
        page_created=False
        if enabled and not self._ingress_first:
            try:
                page_created=self._connect_webgpt(enabled,delivery_adapters)
            except Exception as exc:
                self.runtime_log.write("ERROR",component="runtime",stage="WEBGPT_CONNECT",error=f"{type(exc).__name__}: {exc}")
                if self.telegram_receiver is None:
                    raise
                if self._pw is not None:
                    try: self._pw.stop()
                    except Exception: pass
                    self._pw=None
        elif enabled and self._ingress_first:
            self.runtime_log.write(
                'CONNECT',component='runtime',stage='WEBGPT_ATTACH_DEFERRED',
                detail='Telegram ingress is active; waiting for current LocalAgent host state',
            )
        if not delivery_adapters:
            raise RuntimeError('no_remote_transport_configured')
        self.delivery=DeliveryManager(self.event_store,delivery_adapters)
        self.worker_launcher=RemoteWorkerLauncher(
            queue=self.queue,root=ROOT,cdp=self.cdp,
            allowed_bindings=self._allowed_worker_bindings,
            max_workers=1,
            runtime_log=self.runtime_log,event_store=self.event_store,
            auth_state_provider=None,
        )
        self._start_telegram_ingress()
        self._write_state("RUNNING",page_created=page_created,telegram_enabled=bool(self.telegram_receiver))
        self.runtime_log.write("CONNECT",component="runtime",stage="CONNECTED",cdp=self.cdp,page_created=page_created,telegram_enabled=bool(self.telegram_receiver))
    def _attach_local_browser_when_ready(self,force=False):
        """Upgrade ingress-only Telegram startup after LocalAgent exposes CDP."""
        if self._browser_context() is not None:
            return True
        if getattr(self,'_browser',None) is not None or getattr(self,'_pw',None) is not None:
            self._discard_stale_browser('cdp_context_unavailable')
        if bool(getattr(self,'_ingress_first',False)):
            try:
                host_state=json.loads(agent_host_state_path().read_text(encoding='utf-8'))
            except Exception:
                return False
            expected_token=os.environ.get('SMARTAGENT_SUPERVISOR_TOKEN','')
            if (
                host_state.get('status') not in {'ready','idle','busy'}
                or not str(host_state.get('cdp_endpoint','') or '')
                or not expected_token
                or str(host_state.get('supervisor_token','') or '')!=expected_token
            ):
                return False
        now=time.monotonic()
        if not force and now<float(getattr(self,'_next_webgpt_attach_at',0.0) or 0.0):
            return False
        self._next_webgpt_attach_at=now+float(getattr(self,'_webgpt_attach_retry_sec',2.0) or 2.0)
        enabled=self._collapse_enabled_aliases(
            self.registry.list_remote_conversations(enabled_only=True)
        )
        adapters=self.delivery.adapters if self.delivery is not None else {}
        try:
            if enabled:
                self._connect_webgpt(enabled,adapters)
            else:
                # Telegram-only ingress still needs an authenticated browser
                # snapshot before an isolated Agent1 worker can be launched.
                self._pw=sync_playwright().start()
                self.runtime_log.write('CONNECT',component='runtime',stage='LAZY_PLAYWRIGHT_READY',cdp=self.cdp)
                self._browser=self._pw.chromium.connect_over_cdp(self.cdp,timeout=10000)
                if not self._browser.contexts:
                    raise RuntimeError('no_browser_context')
            self._worker_waiting_for_browser_logged=False
            self._ingress_first=False
            self.runtime_log.write(
                'RECONNECT',component='runtime',stage='LOCAL_BROWSER_ATTACHED',
                cdp=self.cdp,webgpt_enabled=bool(self.adapter),
            )
            return True
        except Exception as exc:
            self.runtime_log.write(
                'RECONNECT',component='runtime',stage='WAITING_LOCAL_BROWSER',
                cdp=self.cdp,error=f'{type(exc).__name__}: {exc}',
            )
            if self._pw is not None:
                try: self._pw.stop()
                except Exception: pass
            self._pw=None; self._browser=None
            return False
    def _observer(self,rec):
        gateway=AgentIngressGateway(
            task_queue=self.queue,session_router=self.session_router,
            event_store=self.event_store,runtime_log=self.runtime_log,
        )
        return RemoteIngressObserver(registry=self.registry,task_queue=self.queue,adapter=self.adapter,workspace=rec['workspace'],conversation_url=rec['gpt_url'],event_store=self.event_store,runtime_log=self.runtime_log,ingress_gateway=gateway)
    def _poll_record(self,rec,*,navigate:bool):
        self.runtime_log.write("POLL",component="runtime",conversation_url=rec['gpt_url'],navigate=bool(navigate))
        if navigate and self.adapter.open_or_reconcile(rec['gpt_url'])!=CONNECTED: raise RuntimeError('reconcile_unavailable')
        obs=self._observer(rec)
        if not self.registry.get_watch_cursor(rec['gpt_url'],CURSOR_NAME): obs.baseline(); return []
        return obs.poll()
    def _retry_delivery(self):
        """Retry durable events without turning one store failure into Agent0 exit."""
        if self.delivery is None: return
        try:
            # Non-browser replies never need to wait for page ownership.
            results=self.delivery.retry_ready(transports={'TELEGRAM','LOCAL_TEST'})
            if not self._execution_work_waiting():
                try:
                    with execution_page_lease(timeout_sec=0.1,label='RemoteAgent event delivery'):
                        results.extend(self.delivery.retry_ready(
                            transports={'WEBGPT','WEBGPT_COPILOT'},
                            should_continue=lambda: not self._execution_work_waiting(),
                            max_events=1,
                        ))
                except RuntimeError as exc:
                    if 'lock timeout' not in str(exc):
                        raise
        except Exception as exc:
            self.runtime_log.write("ERROR",component="runtime",stage="DELIVERY_RETRY",error=f"{type(exc).__name__}: {exc}")
            return
        for event_id,result in results:
            event=self.event_store.events.get(event_id)
            self.runtime_log.write("DELIVERY",component="runtime",event_id=event_id,event_type=getattr(event,"event_type","") if event else "",delivered=bool(result.get("delivered")),reason=str(result.get("reason","") or ""))
            if event is None or not result.get("delivered"):
                continue
            if event.event_type in {"TASK_COMPLETED","TASK_FAILED","TASK_INTERRUPTED"}:
                try:
                    self.queue.record_result_delivery(event.task_id,delivered=True,reply=str(result.get("reply","") or ""),error="")
                except Exception as exc:
                    self.runtime_log.write("ERROR",component="runtime",stage="DELIVERY_RECORD",event_id=event_id,error=f"{type(exc).__name__}: {exc}")
    def _dispatch_workers(self):
        """Keep one queue/event persistence failure inside the current tick."""
        if self.worker_launcher is None: return
        if self._browser_context() is None:
            try:
                # Reconciliation is storage/process supervision and must not
                # depend on a usable browser.  This closes the recovery gap
                # after Agent0 or a request-scoped worker is restarted.
                self.worker_launcher.reconcile_workers()
            except Exception as exc:
                self.runtime_log.write(
                    "ERROR",component="runtime",stage="WORKER_RECONCILE",
                    error=f"{type(exc).__name__}: {exc}",
                )
            if not bool(getattr(self,'_worker_waiting_for_browser_logged',False)):
                self.runtime_log.write(
                    'POLL',component='worker_launcher',stage='WAITING_LOCAL_BROWSER',
                    detail='queued tasks remain durable until authenticated CDP is ready',
                )
                self._worker_waiting_for_browser_logged=True
            return
        try:
            self.worker_launcher.dispatch_available()
        except Exception as exc:
            self.runtime_log.write("ERROR",component="runtime",stage="WORKER_DISPATCH",error=f"{type(exc).__name__}: {exc}")
    def tick(self):
        self._attach_local_browser_when_ready()
        # The Telegram listener/local sender persists tasks out of process.
        # Refresh once per tick so page ownership and dispatch decisions see
        # requests accepted since this runtime was started.
        with self.queue.store.process_lock():
            self.queue.store.load()
        self._write_state(
            "RUNNING",
            telegram_enabled=bool(self.telegram_receiver),
            webgpt_enabled=bool(self.adapter),
        )
        current=str(getattr(getattr(self.scraper,'_page',None),'url','') or '')
        enabled=self._collapse_enabled_aliases(
            self.registry.list_remote_conversations(enabled_only=True),current
        )
        # A queued request already owns the execution conversation.  Do not let
        # Agent0's observer/scheduler navigate that page between ingress and
        # worker dispatch; doing so made the direct worker open a second tab and
        # could strand the request in ChatGPT navigation.
        worker_launcher=getattr(self,'worker_launcher',None)
        execution_busy=bool(
            self.queue.queued()
            or self.queue.running()
            or (
                worker_launcher is not None
                and worker_launcher._processes
            )
        )
        current_identity=self._conversation_identity(current)
        active=next((r for r in enabled if self._conversation_identity(r['gpt_url'])==current_identity),None)
        if (
            not execution_busy
            and self.adapter is not None
            and not bool(getattr(self,'_demand_scoped',False))
        ):
            try:
                with execution_page_lease(timeout_sec=0.1,label="RemoteAgent observer page"):
                    # Recheck after acquiring the cross-process lease. A task
                    # may have arrived while Agent0 was waiting for a Worker.
                    if not (self.queue.queued() or self.queue.running()):
                        if active:
                            try: self._poll_record(active,navigate=False)
                            except Exception as exc:
                                if self._closed_page_error(exc) and self._recover_webgpt_page(enabled,reason='ACTIVE_POLL'):
                                    pass
                                else:
                                    self.runtime_log.write("ERROR",component="runtime",stage="ACTIVE_POLL",conversation_url=active['gpt_url'],error=f"{type(exc).__name__}: {exc}")
                        # Agent0 owns one persistent execution page for its whole
                        # lifetime.  Never rotate that page through other registry
                        # conversations while idle: the next Telegram worker must
                        # write into the same composer instead of opening a tab.
            except RuntimeError as exc:
                if "lock timeout" not in str(exc):
                    raise
                self.runtime_log.write(
                    "POLL",component="runtime",stage="EXECUTION_PAGE_LEASE_BUSY"
                )
        self._dispatch_workers()
        self._retry_delivery()
        self._write_state(
            "RUNNING",
            telegram_enabled=bool(self.telegram_receiver),
            webgpt_enabled=bool(self.adapter),
            worker_state_known=worker_launcher is not None,
            active_worker_count=(
                len(worker_launcher._processes)
                if worker_launcher is not None else 0
            ),
            demand_scoped=bool(getattr(self,'_demand_scoped',False)),
        )
    def run(self):
        self.connect()
        self._start_heartbeat()
        try:
            while self._parent_alive(): self.tick(); time.sleep(self.poll)
        except Exception as exc:
            self.runtime_log.write("ERROR",component="runtime",stage="RUN",error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            self._stop_heartbeat()
            self._write_state("STOPPED")
            if self.telegram_receiver is not None:
                self.telegram_receiver.stop()
            self._close_owned_page()
            if self._pw is not None: self._pw.stop()

    def _close_owned_page(self):
        """Close only a page created by Agent0, never a claimed user page."""
        page=self._owned_page
        created=bool(self._owned_page_created)
        self._owned_page=None; self._owned_page_created=False
        if page is not None and created:
            try: page.close()
            except Exception: pass
