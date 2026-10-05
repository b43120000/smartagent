#!/usr/bin/env python3
"""Isolated Git worktree transaction for guarded LocalAgent self repair."""
from __future__ import annotations
import hashlib,json,os,subprocess,time,uuid
from pathlib import Path,PurePosixPath
from typing import Any
from .paths import self_repair_root

def allowed_repair_path(value:str)->bool:
    v=str(value or "").replace("\\","/").lstrip("./")
    if not v or ":" in v or v.startswith("/") or ".." in PurePosixPath(v).parts:return False
    if v == "smart_agent.py":return True
    if v.startswith("agent_core/") and v.endswith(".py"):return True
    return v.startswith("tests/") and PurePosixPath(v).name.startswith("validate_") and v.endswith(".py")

class RepairTransaction:
    def __init__(self,repo:str|Path,state_root:str|Path|None=None):
        self.repo=Path(repo).resolve();self.state_root=Path(state_root or self_repair_root(self.repo));self.repair_id="REPAIR-"+uuid.uuid4().hex[:10].upper();self.worktree=self.state_root/"worktrees"/self.repair_id;self.branch="self-repair/"+self.repair_id.lower();self.base="";self.modified=[]
    def _git(self,*args,check=True,cwd=None):return subprocess.run(["git",*args],cwd=str(cwd or self.repo),capture_output=True,text=True,check=check,timeout=30)
    def begin(self,allow_dirty:bool=False)->dict:
        if not allow_dirty and self._git("status","--porcelain").stdout.strip():raise RuntimeError("dirty_repository_requires_backup")
        self.base=self._git("rev-parse","HEAD").stdout.strip();self.worktree.parent.mkdir(parents=True,exist_ok=True)
        self._git("worktree","add","-b",self.branch,str(self.worktree),self.base)
        return self.manifest("STAGED")
    def write(self,relative_path:str,content:str):
        if not allowed_repair_path(relative_path):raise ValueError("repair_path_out_of_scope")
        p=(self.worktree/relative_path).resolve()
        if self.worktree not in p.parents:raise ValueError("repair_path_out_of_scope")
        p.parent.mkdir(parents=True,exist_ok=True);p.write_text(content,encoding="utf-8");self.modified.append(relative_path.replace("\\","/"))
    def validate(self,commands:list[list[str]])->dict:
        results=[];ok=True
        for command in commands:
            if not isinstance(command,list) or not command:raise ValueError("validation_command_must_be_argv")
            r=subprocess.run(command,cwd=str(self.worktree),capture_output=True,text=True,timeout=120,check=False)
            results.append({"command":command,"returncode":r.returncode,"stdout":r.stdout[-4000:],"stderr":r.stderr[-4000:]});ok=ok and r.returncode==0
        state="VALIDATED" if ok else "FAILED";m=self.manifest(state,validation=results);return {**m,"passed":ok}
    def commit_candidate(self,message:str="self-repair candidate")->dict:
        self._git("add","--",*sorted(set(self.modified)),cwd=self.worktree);self._git("commit","-m",message,cwd=self.worktree)
        candidate=self._git("rev-parse","HEAD",cwd=self.worktree).stdout.strip();return self.manifest("CANDIDATE",candidate_revision=candidate)
    def manifest(self,state:str,**extra)->dict:
        data={"version":1,"repair_id":self.repair_id,"state":state,"repo":str(self.repo),"worktree":str(self.worktree),"branch":self.branch,"known_good_revision":self.base,"modified_paths":sorted(set(self.modified)),"updated_at":time.time(),**extra};self.state_root.mkdir(parents=True,exist_ok=True);p=self.state_root/"active_repair.json";t=p.with_name(p.name+".tmp");t.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8");t.replace(p);return data
    def rollback(self):
        if self.worktree.exists():self._git("worktree","remove","--force",str(self.worktree),check=False)
        self._git("branch","-D",self.branch,check=False);return self.manifest("ROLLED_BACK")

