"""Git49 OS Broker requests are non-authorizing and narrowly bound."""
from __future__ import annotations
from pathlib import Path
import pytest
from agent_core import turbo_sandbox
from agent_core.turbo_broker_scope import (
    SCHEMA, TurboBrokerScopeError, prepare_scope_request,
)


@pytest.fixture
def active(tmp_path,monkeypatch):
    ws=tmp_path/"workspace"; root=ws/"turbo"; nested=root/"sub"
    nested.mkdir(parents=True)
    policy={"revision":42,"turbo_requested":True,"turbo_roots":[str(root)],
            "denied_write_roots":[]}
    access={"policy":policy,"turbo_roots":(str(root),),
            "writable_workspaces":(str(ws),),"read_only_roots":()}
    mode={"value":"on"}
    monkeypatch.setattr(turbo_sandbox,"access_snapshot",lambda: access)
    monkeypatch.setattr(turbo_sandbox,"acl_mode",lambda: mode["value"])
    return ws,root,nested,policy,mode


def prepared(active,**changed):
    ws,root,nested,policy,_=active
    args=dict(workspace=ws,working_directory=nested,expected_revision=42,
              task_id="TASK-49",action_id="ACT-49",kind="RUN_COMMAND",
              target=nested/"future.txt")
    args.update(changed)
    return prepare_scope_request(**args)


def test_valid_exact_revision_scope_is_still_not_an_os_grant(active):
    ws,root,nested,_,_=active
    p=prepared(active)
    assert p["schema"]==SCHEMA
    assert p["workspace_root"]==str(ws)
    assert p["turbo_root"]==str(root)
    assert p["working_directory"]==str(nested)
    assert p["target"]==str(nested/"future.txt")
    assert p["policy_revision"]==42
    assert p["execution_status"]=="PREPARED_NOT_EXECUTED"
    assert p["authorization_granted"] is False
    assert p["os_token_verified"] is False
    assert p["os_acl_attested"] is False
    assert p["broker_lease"]==""
    assert len(p["content_sha256"])==64


@pytest.mark.parametrize("kind",["","EXECUTE_ANYTHING","IMPORT_SECRETS"])
def test_unsupported_ops_rejected(active,kind):
    with pytest.raises(TurboBrokerScopeError,match="OPERATION_UNSUPPORTED"):
        prepared(active,kind=kind)


@pytest.mark.parametrize("revision",[41,43,0,"42",None])
def test_stale_or_invalid_revision_rejected(active,revision):
    with pytest.raises(TurboBrokerScopeError,match="REVISION"):
        prepared(active,expected_revision=revision)


def test_outside_cwd_and_target_blocked(active,tmp_path):
    ws,root,nested,_,_=active
    sibling=ws/"other";sibling.mkdir()
    with pytest.raises(TurboBrokerScopeError,match="OUTSIDE_ROOT"):
        prepared(active,working_directory=sibling)
    with pytest.raises(TurboBrokerScopeError,match="OUTSIDE_ROOT"):
        prepared(active,target=sibling/"secret.txt")


def test_parent_traversal_blocked_before_canonicalization(active):
    ws,root,nested,_,_=active
    with pytest.raises(TurboBrokerScopeError,match="PARENT_TRAVERSAL"):
        prepared(active,target=nested/".."/"other.txt")


def test_symlink_junction_escape_blocked(active,tmp_path):
    ws,root,nested,_,_=active
    outside=tmp_path/"other";outside.mkdir()
    link=root/"link"
    try:
        link.symlink_to(outside,target_is_directory=True)
    except (OSError,NotImplementedError):
        pytest.skip("filesystem cannot create symlink without privileges")
    with pytest.raises(TurboBrokerScopeError,match="PATH_UNSAFE"):
        prepared(active,target=link/"escaped.txt")


def test_acl_off_or_turbo_off_forbids_broker_request(active):
    ws,root,nested,policy,mode=active
    for m,q in [("off",True),("on",False)]:
        mode["value"]=m
        policy["turbo_requested"]=q
        with pytest.raises(TurboBrokerScopeError,match="NOT_REQUESTED"):
            prepared(active)


def test_unknown_acl_cannot_authorize(active):
    ws,root,nested,policy,mode=active
    mode["value"]="unknown"
    with pytest.raises(TurboBrokerScopeError,match="ACL_MODE_UNVERIFIED"):
        prepared(active)


def test_action_identity_mandatory(active):
    for key in ["task_id","action_id"]:
        with pytest.raises(TurboBrokerScopeError,match="ACTION_ID_INVALID"):
            prepared(active,**{key:""})
