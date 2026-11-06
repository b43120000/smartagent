"""Restart creation policy and native inherited-Job regression; no Telegram I/O."""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SOURCE))
from RemoteAgent import remote_restart as rr

@unittest.skipUnless(os.name == "nt", "Windows process creation policy")
class RestartJobTests(unittest.TestCase):
    def error(self, code=5):
        exc = PermissionError("native denial")
        exc.winerror = code
        return exc

    def spawn(self, popen):
        return rr.spawn_restart_helper(root=SOURCE.parent, old_pid=os.getpid(),
            old_generation="test", chat_id=1, reply_to_message_id=0, popen=popen)

    def test_inherited_denial_retries_only_without_breakaway(self):
        from unittest.mock import Mock
        popen = Mock(side_effect=[self.error(), NS(pid=123)])
        with patch.object(rr, "ensure_current_process_kill_job", return_value=NS(
                active=True, error="inherited_job", handle=None)), patch.object(rr, "_log"):
            self.assertEqual(self.spawn(popen), 123)
        first, second = popen.call_args_list
        self.assertEqual(first.args, second.args)
        self.assertEqual(first.kwargs["creationflags"] ^ second.kwargs["creationflags"],
                         subprocess.CREATE_BREAKAWAY_FROM_JOB)

    def test_owned_job_and_other_denials_do_not_retry(self):
        from unittest.mock import Mock
        for code, job in [(5, NS(active=True, error="", handle=123)),
                          (5, NS(active=False, error="failure", handle=None)),
                          (1314, NS(active=True, error="inherited_job", handle=None))]:
            popen = Mock(side_effect=self.error(code))
            with patch.object(rr, "ensure_current_process_kill_job", return_value=job):
                with self.assertRaises(PermissionError): self.spawn(popen)
            self.assertEqual(popen.call_count, 1)

    def test_success_does_not_retry(self):
        from unittest.mock import Mock
        popen = Mock(return_value=NS(pid=123))
        self.assertEqual(self.spawn(popen), 123)
        self.assertEqual(popen.call_count, 1)

    def test_native_inherited_job_helper_survives_host_exit(self):
        from ctypes import wintypes as w
        class Basic(ctypes.Structure):
            _fields_=[("user",ctypes.c_longlong),("job",ctypes.c_longlong),
                ("flags",w.DWORD),("min",ctypes.c_size_t),("max",ctypes.c_size_t),
                ("active",w.DWORD),("affinity",ctypes.c_size_t),
                ("priority",w.DWORD),("scheduling",w.DWORD)]
        class Extended(ctypes.Structure):
            _fields_=[("basic",Basic),("io",ctypes.c_ulonglong*6),("memory",ctypes.c_size_t*4)]
        k=ctypes.WinDLL("kernel32",use_last_error=True)
        k.CreateJobObjectW.restype=w.HANDLE
        k.SetInformationJobObject.argtypes=[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD]
        k.AssignProcessToJobObject.argtypes=[w.HANDLE,w.HANDLE]
        k.CloseHandle.argtypes=[w.HANDLE]
        child = """import sys,time
from pathlib import Path
from agent_core.process_file_lock import _pid_alive
while _pid_alive(int(sys.argv[1])): time.sleep(.05)
Path(sys.argv[2]).write_text('survived',encoding='utf-8')
"""
        worker = """import sys,os,subprocess,json
sys.stdin.readline()
from RemoteAgent import remote_restart as rr
from agent_core.windows_process_job import ensure_current_process_kill_job
job=ensure_current_process_kill_job()
assert job.error=='inherited_job'
attempts=[]
def launch(cmd,**kw):
    attempts.append(kw['creationflags'])
    return subprocess.Popen([sys.executable,'-c',sys.argv[1],str(os.getpid()),sys.argv[2]],
        stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,**kw)
rr._log=lambda *a: None
pid=rr.spawn_restart_helper(root=os.getcwd(),old_pid=os.getpid(),old_generation='test',
    chat_id=1,reply_to_message_id=0,popen=launch)
print(json.dumps(attempts))
"""
        # Parent retains the external Job handle while the simulated host exits.
        for allowed in (False, True):
            with self.subTest(allowed=allowed), tempfile.TemporaryDirectory() as tmp:
                marker=Path(tmp)/"survived.txt"
                h=k.CreateJobObjectW(None,None)
                self.assertTrue(h)
                try:
                    info=Extended();info.basic.flags=0x2000 | (0x800 if allowed else 0)
                    self.assertTrue(k.SetInformationJobObject(h,9,ctypes.byref(info),ctypes.sizeof(info)))
                    env={**os.environ,"PYTHONPATH":str(SOURCE)}
                    proc=subprocess.Popen([sys.executable,"-c",worker,child,str(marker)],
                        cwd=SOURCE.parent,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,text=True,env=env)
                    self.assertTrue(k.AssignProcessToJobObject(h,w.HANDLE(int(proc._handle))))
                    out,err=proc.communicate("go\n",timeout=15)
                    self.assertEqual(proc.returncode,0,err)
                    attempts=json.loads(out)
                    self.assertEqual(len(attempts),1 if allowed else 2)
                    import time
                    deadline=time.monotonic()+10
                    while not marker.exists() and time.monotonic()<deadline: time.sleep(.05)
                    self.assertTrue(marker.exists(),"helper died with host")
                finally:
                    k.CloseHandle(h)

if __name__ == "__main__": unittest.main()
