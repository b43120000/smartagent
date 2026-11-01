"""Canonical, fail-closed feature configuration for LocalAgent, RemoteAgent and WebDirect.

Policy (tracked): config/feature_flags.defaults.json.
User values (ignored by Git): config/feature_flags.json.
Precedence: defaults < user values < per-agent user overrides < explicit process env.
An inherited, already resolved worker environment is not reinterpreted as a new
user override. Capability and dependency gates apply LAST and cannot be bypassed.

GAR_V1 deliberately remains derived by gar_webgpt2_settings.apply_target().
No runtime identities, security settings, credentials or test bypasses live here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Mapping, MutableMapping

from .paths import install_root, log_root
from .process_file_lock import exclusive_process_lock

SCHEMA = "SMARTAGENT_FEATURE_CONFIG_V1"
AGENTS = ("local_agent", "remote_agent", "web_direct")
INHERITED_KEY = "SMARTAGENT_FEATURE_CONFIG_RESOLUTION_V1"
_TRUES = frozenset(("1", "true", "yes", "on"))
_FALSES = frozenset(("0", "false", "no", "off"))


def defaults_path(root: str | Path | None = None) -> Path:
    return install_root(root) / "config" / "feature_flags.defaults.json"


def user_path(root: str | Path | None = None) -> Path:
    return install_root(root) / "config" / "feature_flags.json"


def _parse_bool(value: object, *, key: str) -> bool:
    if type(value) is bool:
        return value
    if isinstance(value, str):
        candidate = value.strip().lower()
        if candidate in _TRUES:
            return True
        if candidate in _FALSES:
            return False
    raise ValueError("invalid_feature_boolean:" + key)


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise ValueError("feature_config_unreadable:" + path.name) from exc
    if not isinstance(value, dict):
        raise ValueError("feature_config_root_not_object:" + path.name)
    return value


def load_registry(*, path: Path | None = None) -> dict:
    data = _read_json(Path(path) if path is not None else defaults_path())
    if data.get("schema") != SCHEMA:
        raise ValueError("feature_defaults_schema_invalid")
    features = data.get("features")
    if not isinstance(features, dict) or not features:
        raise ValueError("feature_defaults_missing")
    keys = set()
    for name, spec in features.items():
        if not isinstance(name, str) or not isinstance(spec, dict):
            raise ValueError("feature_defaults_invalid_entry")
        env_name = spec.get("env")
        if not isinstance(env_name, str) or not env_name.isupper() or env_name in keys:
            raise ValueError("feature_defaults_env_conflict")
        keys.add(env_name)
        _parse_bool(spec.get("default"), key=name)
        agents = spec.get("agents")
        if not isinstance(agents, list) or not agents or any(a not in AGENTS for a in agents):
            raise ValueError("feature_defaults_agents_invalid:" + name)
        deps = spec.get("requires")
        if not isinstance(deps, list) or any(d not in features or d == name for d in deps):
            raise ValueError("feature_defaults_dependencies_invalid:" + name)
    return data


def load_user_values(*, path: Path | None = None, registry: dict | None = None) -> dict:
    target = Path(path) if path is not None else user_path()
    if not target.is_file():
        return {"schema": SCHEMA, "features": {}, "agent_overrides": {}}
    value = _read_json(target)
    if value.get("schema") != SCHEMA:
        raise ValueError("feature_user_schema_invalid")
    valid = (registry if registry is not None else load_registry())["features"]
    values = value.get("features", {})
    overrides = value.get("agent_overrides", {})
    if not isinstance(values, dict) or not isinstance(overrides, dict):
        raise ValueError("feature_user_shape_invalid")
    if any(key not in valid or type(v) is not bool for key, v in values.items()):
        raise ValueError("feature_user_unknown_or_invalid_feature")
    if any(a not in AGENTS or not isinstance(items, dict)
           or any(k not in valid or type(v) is not bool for k, v in items.items())
           for a, items in overrides.items()):
        raise ValueError("feature_user_unknown_or_invalid_override")
    return {"schema": SCHEMA, "features": values, "agent_overrides": overrides}


def _inherited_sources(values: Mapping[str, str], registry: dict) -> dict:
    try:
        inherited = json.loads(str(values.get(INHERITED_KEY, "") or ""))
        if inherited.get("schema") != SCHEMA:
            return {}
        keys = {spec["env"] for spec in registry["features"].values()}
        originals = inherited.get("values", {})
        if not isinstance(originals, dict) or not isinstance(inherited.get("sources"), dict):
            return {}
        if any(k not in keys or str(values.get(k, "")) != str(v)
               for k, v in originals.items()):
            return {}
        return dict(inherited["sources"])
    except (ValueError, TypeError, AttributeError):
        return {}


def resolve_features(agent: str, *, environ: Mapping[str, str] | None = None,
                     defaults_file: Path | None = None,
                     user_file: Path | None = None) -> dict:
    if agent not in AGENTS:
        raise ValueError("feature_agent_invalid")
    registry = load_registry(path=defaults_file)
    user = load_user_values(path=user_file, registry=registry)
    values = os.environ if environ is None else environ
    inherited = _inherited_sources(values, registry)
    effective, sources, env_names = {}, {}, {}
    for name, spec in registry["features"].items():
        key = spec["env"]
        env_names[name] = key
        value = bool(spec["default"])
        source = "tracked_default"
        if name in user["features"]:
            value, source = user["features"][name], "user_config"
        if name in user["agent_overrides"].get(agent, {}):
            value, source = user["agent_overrides"][agent][name], "agent_override:" + agent
        if key in values:
            value = _parse_bool(values[key], key=key)
            source = inherited.get(name, "process_environment")
        if agent not in spec["agents"]:
            value, source = False, "unsupported_agent:" + agent
        effective[name], sources[name] = value, source
    # Dependency graph is validated and resolved to its fixed point; cycles
    # without an enabled root safely converge to disabled, never grant access.
    for _ in range(len(effective)):
        changed = False
        for name, spec in registry["features"].items():
            if effective[name] and any(not effective[dep] for dep in spec["requires"]):
                effective[name] = False
                sources[name] = "dependency_disabled:" + ",".join(
                    dep for dep in spec["requires"] if not effective[dep]
                )
                changed = True
        if not changed:
            break
    setting_digest = hashlib.sha256(json.dumps(
        {"registry": registry, "user": user}, ensure_ascii=True,
        sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    effective_digest = hashlib.sha256(json.dumps(
        {"agent": agent, "flags": effective}, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()
    return {"schema": SCHEMA, "agent": agent, "flags": effective,
            "sources": sources, "env_names": env_names,
            "config_sha256": setting_digest,
            "effective_sha256": effective_digest,
            "gar_v1": "DERIVED_FROM_WEBGPT2_TARGET"}


def apply_process_config(agent: str, *, environ: MutableMapping[str, str] | None = None,
                         defaults_file: Path | None = None, user_file: Path | None = None,
                         write_log: bool = True) -> dict:
    env = os.environ if environ is None else environ
    snapshot = resolve_features(agent, environ=env, defaults_file=defaults_file,
                                user_file=user_file)
    applied = {}
    for name, key in snapshot["env_names"].items():
        value = "1" if snapshot["flags"][name] else "0"
        env[key] = value
        applied[key] = value
    # The worker inherits a resolved snapshot; its provenance remains the
    # parent's persisted/explicit source when the values match.
    env[INHERITED_KEY] = json.dumps(
        {"schema": SCHEMA, "values": applied, "sources": snapshot["sources"]},
        sort_keys=True, separators=(",", ":"))
    if write_log:
        log_feature_config(snapshot)
    return snapshot


def log_feature_config(snapshot: Mapping[str, object], *,
                       root: str | Path | None = None) -> None:
    """Best-effort diagnostics with only booleans/digests/sources, no secrets."""
    row = {"event": "FEATURE_FLAGS_EFFECTIVE", "epoch": time.time(),
           "pid": os.getpid(), "schema": SCHEMA,
           "agent": snapshot["agent"], "flags": snapshot["flags"],
           "sources": snapshot["sources"],
           "config_sha256": snapshot["config_sha256"],
           "effective_sha256": snapshot["effective_sha256"],
           "gar_v1": "DERIVED_FROM_WEBGPT2_TARGET"}
    try:
        path = log_root(root) / "feature_flags_runtime.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    except (OSError, ValueError):
        pass


def save_feature(name: str, enabled: bool, *, agent: str = "",
                 path: Path | None = None, defaults_file: Path | None = None) -> dict:
    registry = load_registry(path=defaults_file)
    if name not in registry["features"] or type(enabled) is not bool:
        raise ValueError("feature_setting_invalid")
    if agent and agent not in AGENTS:
        raise ValueError("feature_agent_invalid")
    target = Path(path) if path is not None else user_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_process_lock(target.with_suffix(".lock"), timeout_sec=10,
                                label="feature config update"):
        user = load_user_values(path=target, registry=registry)
        dest = user["agent_overrides"].setdefault(agent, {}) if agent else user["features"]
        dest[name] = enabled
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(user, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        tmp.replace(target)
    return user


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SmartAgent feature config registry")
    parser.add_argument("--agent", choices=AGENTS, required=True)
    parser.add_argument("--show", action="store_true")
    args = parser.parse_args(argv)
    result = resolve_features(args.agent)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "SCHEMA", "AGENTS", "INHERITED_KEY", "defaults_path", "user_path",
    "load_registry", "load_user_values", "resolve_features",
    "apply_process_config", "save_feature", "log_feature_config",
]
