"""Provider-owned DOM selectors and verified dialogue capabilities."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
from urllib.parse import urlparse
import uuid

from .profile_store import _atomic_write, _validate_selector
from .provider_registry import known_providers, provider_from_url, is_other_provider

SCHEMA = "SMARTAGENT_MANUAL_DOM_V1"
ELEMENT_FIELDS = {"composer": "composer_selector", "send": "send_selector",
                  "user_turn": "user_turn_selector", "user_content": "user_content_selector",
                  "assistant_turn": "assistant_turn_selector",
                  "assistant_content": "final_content_selector", "stop": "stop_selectors"}
VALIDATION_KEYS = {"composer": "editable_unique", "send": "click_verified",
                   "user_turn": "marker_verified", "user_content": "content_verified",
                   "assistant_turn": "ownership_verified", "assistant_content": "content_verified",
                   "stop": "stop_verified"}
CAPABILITY_KEYS = {'generation_state':'lifecycle_verified', 'blocking_state':'rules_verified',
                   'conversation_loop':'loop_verified'}


def config_path(provider: str, *, root: Path | None = None) -> Path:
    if provider not in known_providers() and not is_other_provider(provider):
        raise ValueError("unsupported_provider:" + provider)
    base = Path(root) if root else Path(__file__).parent / "providers"
    return base / "other" / provider / "dom_config.json" if is_other_provider(provider) else base / provider / "dom_config.json"


def validate_document(document: dict, provider: str) -> dict:
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        raise ValueError("manual_dom_schema_mismatch")
    if document.get("provider") != provider:
        raise ValueError("manual_dom_provider_mismatch")
    if provider_from_url(document.get("source_origin", "")) != provider:
        raise ValueError("manual_dom_origin_mismatch")
    elements = document.get("elements")
    if not isinstance(elements, dict) or "composer" not in elements or set(elements) - set(ELEMENT_FIELDS):
        raise ValueError("manual_dom_invalid_elements")
    for name, entry in elements.items():
        if not isinstance(entry, dict) or entry.get("validation", {}).get(VALIDATION_KEYS[name]) is not True:
            raise ValueError(f"manual_dom_{name}_not_validated")
        _validate_selector(entry.get("selector"), field=ELEMENT_FIELDS[name])
    if "user_content" in elements and "user_turn" not in elements:
        raise ValueError("manual_dom_user_content_requires_user_turn")
    if "assistant_turn" in elements and not {'user_turn','user_content'}.issubset(elements):
        raise ValueError('manual_dom_assistant_requires_user_binding')
    if 'assistant_content' in elements and 'assistant_turn' not in elements:
        raise ValueError('manual_dom_assistant_content_requires_assistant_turn')
    capabilities=document.get('capabilities',{})
    if not isinstance(capabilities,dict) or set(capabilities)-set(CAPABILITY_KEYS):
        raise ValueError('manual_dom_invalid_capabilities')
    for name,entry in capabilities.items():
        if not isinstance(entry,dict) or entry.get('validation',{}).get(CAPABILITY_KEYS[name]) is not True:
            raise ValueError('manual_dom_capability_not_validated:'+name)
        if name=='generation_state':
            if not {'assistant_content','stop'}.issubset(elements) or entry.get('strategy')!='stop_busy_and_stable_text':
                raise ValueError('manual_dom_invalid_lifecycle')
            if type(entry.get('quiet_seconds')) not in (int,float) or not 2<=entry['quiet_seconds']<=30:
                raise ValueError('manual_dom_invalid_quiet_seconds')
        elif name=='blocking_state':
            from .dialogue_state import validate_blocking_rules
            validate_blocking_rules(entry.get('rules'))
        elif not {'generation_state','blocking_state'}.issubset(capabilities):
            raise ValueError('manual_dom_loop_requires_lifecycle_and_blockers')
    from .file_config import validate_file_config
    validate_file_config(document.get('files', {}))
    return document


def load_dom_config(provider: str, *, root: Path | None = None) -> dict | None:
    path = config_path(provider, root=root)
    if not path.exists():
        return None
    # An explicitly present broken manual config must not silently use old DOM.
    return validate_document(json.loads(path.read_text(encoding="utf-8")), provider)


def save_element(provider: str, url: str, name: str, selector: str, metadata: dict,
                 validation: dict, *, root: Path | None = None) -> Path:
    if provider_from_url(url) != provider:
        raise ValueError("manual_dom_url_provider_mismatch")
    parsed = urlparse(url)
    origin_host = f'[{parsed.hostname}]' if ':' in parsed.hostname else parsed.hostname
    if name not in ELEMENT_FIELDS or validation.get(VALIDATION_KEYS[name]) is not True:
        raise ValueError("manual_dom_element_not_validated:" + name)
    existing = load_dom_config(provider, root=root)
    elements = dict((existing or {}).get("elements", {}))
    capabilities=dict((existing or {}).get('capabilities',{}))
    changed=elements.get(name,{}).get('selector')!=selector
    # A newly selected turn container invalidates content selectors relative to
    # its former root. Other independently verified components remain intact.
    if name == "user_turn" and elements.get(name, {}).get("selector") != selector:
        elements.pop("user_content", None)
        elements.pop("assistant_turn", None)
    if name == 'user_content' and elements.get(name,{}).get('selector') != selector:
        elements.pop('assistant_turn',None)
    if changed:
        capabilities.pop('conversation_loop',None)
        if name in {'composer','send','user_turn','user_content','assistant_turn','assistant_content','stop'}:
            capabilities.pop('generation_state',None)
        if name in {'user_turn','user_content','assistant_turn'}:
            elements.pop('assistant_content',None)
    elements[name] = {
        "selector": _validate_selector(selector, field=ELEMENT_FIELDS[name]),
        "tag": str(metadata.get("tag", "")), "kind": str(metadata.get("kind", name)),
        "scope": "user_turn" if name == "user_content" else "assistant_turn" if name=='assistant_content' else "page",
        "validation": {**validation, "full_conversation": False},
    }
    document = {
        "schema": SCHEMA, "provider": provider,
        "revision": uuid.uuid4().hex,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_origin": f"{parsed.scheme}://{origin_host}" + (f":{parsed.port}" if parsed.port else ""),
        "scope": "composer_only" if set(elements) == {"composer"} else "partial_conversation",
        "elements": elements,
        "capabilities": capabilities,
        "files": dict((existing or {}).get('files', {})),
    }
    if changed:
        document['files'].pop('roundtrip', None)
        if name in {'user_turn','assistant_turn','send'}:
            document['files'].pop('attachment_verified',None)
        if name=='assistant_turn':
            document['files'].pop('download_verified',None)
    validate_document(document, provider)
    path = config_path(provider, root=root)
    if path.exists():
        old = json.loads(path.read_text(encoding="utf-8"))
        _atomic_write(path.parent / "dom_config.history" / f"{uuid.uuid4().hex}.json", old)
    _atomic_write(path, document)
    return path


def save_composer(provider: str, url: str, selector: str, metadata: dict, *, root: Path | None = None) -> Path:
    return save_element(provider, url, "composer", selector, metadata, {"editable_unique": True}, root=root)


def apply_dom_config(provider: str, profile):
    """Overlay verified components; retain uncalibrated profile capabilities."""
    document = load_dom_config(provider)
    if document is None or profile is None:
        return profile
    return replace(profile, **{ELEMENT_FIELDS[name]: (entry['selector'],) if name=='stop' else entry['selector']
                               for name, entry in document["elements"].items()})


def load_capability(provider: str, name: str) -> dict:
    return (load_dom_config(provider) or {}).get('capabilities',{}).get(name,{})


def save_capability(provider: str, name: str, settings: dict, *, root: Path | None = None) -> Path:
    if name not in CAPABILITY_KEYS:raise ValueError('unknown_capability')
    current=load_dom_config(provider,root=root)
    if not current:raise ValueError('manual_dom_config_missing')
    caps=dict(current.get('capabilities',{}));caps[name]=settings
    if name!='conversation_loop':caps.pop('conversation_loop',None)
    document={**current,'capabilities':caps,'revision':uuid.uuid4().hex,
              'updated_at':datetime.now(timezone.utc).isoformat()}
    validate_document(document,provider)
    path=config_path(provider,root=root)
    _atomic_write(path.parent/'dom_config.history'/f'{uuid.uuid4().hex}.json',current)
    _atomic_write(path,document)
    return path
