"""Git63 Global Managed Toolchain Registry. Catalog and receipts are NOT grants."""
from __future__ import annotations
import hashlib, json, os, re
from pathlib import Path
from urllib.parse import urlsplit
from collections.abc import Mapping
from .paths import install_root, environment_ledger_path
from .json_state_io import write_json_atomic
from .process_file_lock import exclusive_process_lock

CATALOG_SCHEMA = "SMARTAGENT_GIT63_CATALOG_V1"
RECEIPT_SCHEMA = "SMARTAGENT_GIT63_INSTALL_RECEIPT_V1"
LEDGER_SCHEMA = "SMARTAGENT_ENVIRONMENT_LEDGER_V1"
TOKEN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
HEX = re.compile(r"^[0-9a-fA-F]{64}$")
HOSTS = frozenset({"github.com","api.adoptium.net","dl.google.com",
    "storage.googleapis.com","services.gradle.org","download.eclipse.org"})

def store_root(root=None):
    return install_root(root) / "localdata" / "toolchains"

def catalog_path(root=None):
    return install_root(root) / "config" / "managed_toolchains_catalog.json"

def sha256_file(path):
    h = hashlib.sha256()
    with path.open("rb") as s:
        for chunk in iter(lambda: s.read(1048576), b""):
            h.update(chunk)
    return h.hexdigest()

def within(path, root):
    try:
        if os.path.commonpath((str(path.absolute()), str(root.absolute()))).casefold() != str(root.absolute()).casefold():
            return False
        cur = path
        while cur != root:
            if cur.is_symlink() or cur.parent == cur:
                return False
            cur = cur.parent
        return not root.is_symlink()
    except (OSError, ValueError):
        return False

def read_catalog(root=None):
    catalog = json.loads(catalog_path(root).read_text(encoding="utf-8"))
    if catalog.get("schema") != CATALOG_SCHEMA or not isinstance(catalog.get("packages"), list) or len(catalog["packages"]) > 128:
        raise ValueError("GIT63_CATALOG_INVALID")
    seen = set()
    for row in catalog["packages"]:
        name, version = row.get("tool"), row.get("version")
        if not TOKEN.fullmatch(str(name)) or not VERSION.fullmatch(str(version)) or (name,version) in seen:
            raise ValueError("GIT63_CATALOG_TOOL_INVALID")
        seen.add((name,version))
        url = urlsplit(str(row.get("url","")))
        if url.scheme != "https" or url.hostname not in HOSTS or url.username or url.password or url.fragment:
            raise ValueError("GIT63_CATALOG_URL_UNTRUSTED")
        if row.get("archive_type") != "zip" or not HEX.fullmatch(str(row.get("archive_sha256",""))):
            raise ValueError("GIT63_CATALOG_ARCHIVE_UNPINNED")
        paths = row.get("executables")
        if not isinstance(paths, dict) or not paths or len(paths) > 32:
            raise ValueError("GIT63_CATALOG_EXECUTABLE_INVALID")
        for alias, relative in paths.items():
            parts = str(relative).replace("\\","/").split("/")
            if not TOKEN.fullmatch(str(alias)) or not str(relative).casefold().endswith((".exe",".bat",".cmd")) or any(p in ("",".","..") for p in parts) or ":" in str(relative) or str(relative).startswith(("/","\\")):
                raise ValueError("GIT63_CATALOG_EXECUTABLE_INVALID")
    return catalog

def select_package(tool, version, root=None):
    if not TOKEN.fullmatch(str(tool)) or not VERSION.fullmatch(str(version)):
        raise ValueError("GIT63_SELECTION_INVALID")
    candidates = [p for p in read_catalog(root)["packages"] if p["tool"] == tool and p["version"] == version]
    if len(candidates) != 1:
        raise ValueError("GIT63_TOOL_NOT_IN_TRUSTED_CATALOG")
    return candidates[0]

def managed_hints(ledger: Mapping, root=None):
    entries = ledger.get("managed_tools", {}) if isinstance(ledger, Mapping) else {}
    output = []
    if not isinstance(entries, dict):
        return output
    for item in list(entries.values())[:128]:
        try:
            path = Path(item["executable"])
            base = store_root(root) / item["tool"] / item["version"]
            if not within(path, base) or not path.is_file() or sha256_file(path) != item["executable_sha256"]:
                continue
            output.append({"name":item["alias"],"tool":item["tool"],"version":item["version"],"path":str(path),
                "availability":"RECORDED_REPROBE_REQUIRED","execution_authorized":False})
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return output

def update_ledger(update, root=None):
    path = environment_ledger_path(root)
    with exclusive_process_lock(path.with_suffix(".git63.lock"), label="git63-ledger"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            payload = {"schema":LEDGER_SCHEMA,"tools":{}}
        if payload.get("schema") != LEDGER_SCHEMA:
            raise ValueError("GIT63_LEDGER_SCHEMA_INVALID")
        answer = update(payload)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(path,payload)
        return answer

def register_install_receipt(receipt, root=None, evidence_ref=""):
    if (receipt.get("schema") != RECEIPT_SCHEMA or receipt.get("status") != "INSTALLED"
            or receipt.get("sandbox_backend") != "WIN32_APPCONTAINER"
            or receipt.get("sandbox_identity_verified") is not True
            or receipt.get("kernel_outside_write_denied") is not True):
        raise ValueError("GIT63_INSTALL_ATTESTATION_REQUIRED")
    tool,version = receipt.get("tool"),receipt.get("version")
    package = select_package(tool,version,root)
    if str(receipt.get("archive_sha256","")).casefold() != package["archive_sha256"].casefold():
        raise ValueError("GIT63_INSTALL_ARCHIVE_MISMATCH")
    items = {}
    for alias, relative in package["executables"].items():
        path = store_root(root)/tool/version/relative.replace("\\","/")
        if not path.is_file() or not within(path,store_root(root)/tool/version):
            raise ValueError("GIT63_INSTALL_EXECUTABLE_UNTRUSTED")
        items[f"{alias}:{version}"] = {"tool":tool,"version":version,"alias":alias,
            "executable":str(path),"executable_sha256":sha256_file(path),
            "evidence_ref":evidence_ref,"archive_sha256":package["archive_sha256"]}
    def save(payload):
        managed = payload.setdefault("managed_tools",{})
        if not isinstance(managed,dict) or len(managed)+len(items)>128:
            raise ValueError("GIT63_MANAGED_RECORD_LIMIT")
        managed.update(items)
        return {"status":"RECORDED_REPROBE_REQUIRED",
                "tool_paths":{k:v["executable"] for k,v in items.items()}}
    return update_ledger(save,root)

def register_external_hint(row,root=None):
    def save(payload):
        hints=payload.setdefault("external_tool_hints",{})
        if not isinstance(hints,dict):
            raise ValueError("GIT63_EXTERNAL_HINTS_INVALID")
        path=str(row.get("path",""))[:1024]
        if path:
            hints[path.casefold()]={"name":str(row.get("name",""))[:100],
                "path":path,"evidence_ref":str(row.get("last_evidence_ref",""))[:160],
                "execution_authorized":False,"reprobe_required":True}
        if len(hints)>128: hints.pop(next(iter(hints)))
    update_ledger(save,root)
