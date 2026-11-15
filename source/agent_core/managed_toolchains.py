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

def validate_source(tool, version, source):
    """Validate a per-task source descriptor, not a preinstalled package list.

    A dynamic install must carry a publisher checksum URL. No shell script,
    command, install target, PATH or arbitrary origin can be supplied here.
    """
    if not TOKEN.fullmatch(str(tool)) or not VERSION.fullmatch(str(version)):
        raise ValueError("GIT63_SELECTION_INVALID")
    if not isinstance(source, Mapping):
        raise ValueError("GIT63_SOURCE_DESCRIPTOR_REQUIRED")
    row = dict(source)
    if set(row) != {"url", "archive_sha256", "checksum_url", "executables", "archive_type"}:
        raise ValueError("GIT63_SOURCE_DESCRIPTOR_FIELDS_INVALID")
    if row["archive_type"] != "zip" or not HEX.fullmatch(str(row["archive_sha256"])):
        raise ValueError("GIT63_SOURCE_ARCHIVE_UNPINNED")
    urls = [urlsplit(str(row[k])) for k in ("url", "checksum_url")]
    for parsed in urls:
        if (parsed.scheme != "https" or parsed.hostname not in HOSTS or
                parsed.username or parsed.password or parsed.fragment or
                not parsed.path or len(parsed.geturl()) > 2048):
            raise ValueError("GIT63_SOURCE_URL_UNTRUSTED")
    if urls[0].hostname != urls[1].hostname:
        raise ValueError("GIT63_SOURCE_CHECKSUM_ORIGIN_MISMATCH")
    executables = row["executables"]
    if not isinstance(executables, dict) or not 1 <= len(executables) <= 32:
        raise ValueError("GIT63_SOURCE_EXECUTABLES_INVALID")
    for alias, relative in executables.items():
        parts = str(relative).replace("\\", "/").split("/")
        if (not TOKEN.fullmatch(str(alias)) or len(str(relative)) > 512 or
                not str(relative).casefold().endswith((".exe", ".bat", ".cmd")) or
                str(relative).startswith(("/", "\\")) or ":" in str(relative) or
                any(part in ("", ".", "..") for part in parts)):
            raise ValueError("GIT63_SOURCE_EXECUTABLE_PATH_INVALID")
    return {"tool": tool, "version": version, **row}


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

def select_package(tool, version, root=None, source=None):
    if not TOKEN.fullmatch(str(tool)) or not VERSION.fullmatch(str(version)):
        raise ValueError("GIT63_SELECTION_INVALID")
    if source is not None:
        return validate_source(tool, version, source)
    candidates = [p for p in read_catalog(root)["packages"]
                  if p["tool"] == tool and p["version"] == version]
    if len(candidates) == 1:
        return candidates[0]
    # A package installed by a previous Task is reusable across all workspaces.
    try:
        ledger = json.loads(environment_ledger_path(root).read_text(encoding="utf-8"))
        recorded = ledger.get("managed_sources", {}).get(f"{tool}:{version}")
        if isinstance(recorded, dict):
            descriptor = {key: recorded[key] for key in
                          ("url", "archive_sha256", "checksum_url",
                           "executables", "archive_type")}
            return validate_source(tool, version, descriptor)
    except (OSError, ValueError, TypeError, KeyError):
        pass
    raise ValueError("GIT63_DYNAMIC_SOURCE_REQUIRED")

def managed_hints(ledger: Mapping, root=None):
    entries = ledger.get("managed_tools", {}) if isinstance(ledger, Mapping) else {}
    sources = ledger.get("managed_sources", {}) if isinstance(ledger, Mapping) else {}
    output = []
    if not isinstance(entries, dict) or not isinstance(sources, dict):
        return output
    for item in list(entries.values())[:128]:
        try:
            tool, version, alias = item["tool"], item["version"], item["alias"]
            source = sources[f"{tool}:{version}"]
            relative = source["executables"][alias].replace("\\", "/")
            path = Path(item["executable"])
            base = store_root(root) / tool / version
            expected = base / relative
            if (not isinstance(source, dict) or
                    item["archive_sha256"] != source["archive_sha256"] or
                    os.path.normcase(str(path.absolute())) !=
                    os.path.normcase(str(expected.absolute())) or
                    not within(path, base) or not path.is_file() or
                    sha256_file(path) != item["executable_sha256"]):
                continue
            output.append({"name":alias, "tool":tool, "version":version, "path":str(path),
                "availability":"RECORDED_REPROBE_REQUIRED","execution_authorized":False})
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
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

def register_install_receipt(receipt, root=None, evidence_ref="", source=None):
    if (receipt.get("schema") != RECEIPT_SCHEMA or receipt.get("status") != "INSTALLED"
            or receipt.get("sandbox_backend") != "WIN32_APPCONTAINER"
            or receipt.get("sandbox_identity_verified") is not True
            or receipt.get("kernel_outside_write_denied") is not True):
        raise ValueError("GIT63_INSTALL_ATTESTATION_REQUIRED")
    tool,version = receipt.get("tool"),receipt.get("version")
    package = select_package(tool, version, root, source=source)
    if source is not None and receipt.get("publisher_checksum_verified") is not True:
        raise ValueError("GIT63_SOURCE_CHECKSUM_ATTESTATION_REQUIRED")
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
        managed = payload.setdefault("managed_tools", {})
        sources = payload.setdefault("managed_sources", {})
        if (not isinstance(managed, dict) or not isinstance(sources, dict)
                or len(managed)+len(items)>128 or len(sources)>128):
            raise ValueError("GIT63_MANAGED_RECORD_LIMIT")
        source_key = f"{tool}:{version}"
        if source_key in sources and sources[source_key] != package:
            raise ValueError("GIT63_MANAGED_VERSION_SOURCE_CONFLICT")
        sources[source_key] = package
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
