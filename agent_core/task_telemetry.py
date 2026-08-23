#!/usr/bin/env python3
from __future__ import annotations
import json,time
from pathlib import Path

class TaskTelemetry:
 def __init__(self,root:str|Path):
  self.root=Path(root);self.run_id='';self.started=0.0;self.events=[];self.counters={}
 def begin(self,run_id:str):
  self.run_id=str(run_id);self.started=time.perf_counter();self.events=[];self.counters={'web_round_trip_count':0,'tool_action_count':0,'file_read_count':0,'uploaded_file_count':0,'build_count':0};self.event('task_received')
 def event(self,name:str,**data):
  self.events.append({'name':str(name),'elapsed_ms':round((time.perf_counter()-self.started)*1000,3) if self.started else 0.0,**data})
 def inc(self,name:str,value:int=1): self.counters[name]=int(self.counters.get(name,0))+int(value)
 def summary(self)->dict:
  total=round((time.perf_counter()-self.started)*1000,3) if self.started else 0.0
  planner=sum(float(e.get('duration_ms',0) or 0) for e in self.events if e.get('name')=='planner_response_end')
  local=sum(float(e.get('duration_ms',0) or 0) for e in self.events if e.get('name')=='tool_action_end')
  build=sum(float(e.get('duration_ms',0) or 0) for e in self.events if e.get('name')=='build_end')
  return {'schema':'TASK_TELEMETRY_V1','run_id':self.run_id,'total_elapsed_ms':total,'planner_elapsed_ms':round(planner,3),'local_io_elapsed_ms':round(local,3),'build_elapsed_ms':round(build,3),**self.counters,'events':list(self.events)}
 def save(self)->dict:
  data=self.summary();self.root.mkdir(parents=True,exist_ok=True);p=self.root/f'{self.run_id}.json';tmp=p.with_name(p.name+'.tmp');tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2,sort_keys=True),encoding='utf-8');tmp.replace(p);return data
