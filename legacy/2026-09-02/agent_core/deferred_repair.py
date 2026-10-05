#!/usr/bin/env python3
"""Task-end bundling for issues recovered during the active task."""
from __future__ import annotations
import json,os,time,uuid
from pathlib import Path
from .workspace import AGENT_PROJECT_ROOT
from .issue_recorder import DEFAULT_EVENT_STORE

class DeferredRepairQueue:
 def __init__(self,event_store:Path=DEFAULT_EVENT_STORE,queue_path:Path|None=None,doc_dir:Path|None=None):
  self.event_store=Path(event_store);self.queue_path=Path(queue_path or AGENT_PROJECT_ROOT/".agents/self_repair/deferred_queue.json");self.doc_dir=Path(doc_dir or AGENT_PROJECT_ROOT/"doc/self_repair/deferred")
 def _load_queue(self):
  try:return json.loads(self.queue_path.read_text(encoding="utf-8"))
  except Exception:return {"version":1,"items":[]}
 def finalize_run(self,run_id:str)->dict:
  events=[]
  try:events=[json.loads(x) for x in self.event_store.read_text(encoding="utf-8").splitlines() if x.strip()]
  except Exception:pass
  selected={e["issue_id"]:e for e in events if e.get("run_id")==run_id and e.get("disposition")=="DEFERRED"}
  if not selected:return {"status":"NO_DEFERRED_ISSUES","count":0}
  q=self._load_queue();existing={x.get("issue_id") for x in q.get("items",[])};added=[]
  for issue,e in selected.items():
   if issue in existing:continue
   item={"queue_id":"DEFER-"+uuid.uuid4().hex[:10].upper(),"issue_id":issue,"run_id":run_id,"state":"QUEUED","created_at":time.time(),"document":f"doc/self_repair/issues/{issue}.md"};q.setdefault("items",[]).append(item);added.append(item)
  self.queue_path.parent.mkdir(parents=True,exist_ok=True);t=self.queue_path.with_name(self.queue_path.name+".tmp");t.write_text(json.dumps(q,ensure_ascii=False,indent=2),encoding="utf-8");t.replace(self.queue_path)
  self.doc_dir.mkdir(parents=True,exist_ok=True);bundle=self.doc_dir/f"{run_id}.md";lines=[f"# Deferred issues for {run_id}","","These issues were recovered during the task. They are queued for a later repair cycle; no hot switch was performed.",""]+[f"- [{x['issue_id']}](../issues/{x['issue_id']}.md)" for x in q["items"] if x.get("run_id")==run_id];bundle.write_text("\n".join(lines)+"\n",encoding="utf-8")
  return {"status":"QUEUED","count":len(added),"bundle":str(bundle)}

