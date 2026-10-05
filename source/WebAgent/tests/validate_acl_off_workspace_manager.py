from __future__ import annotations

import importlib
import json
import shutil
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def _load_workspace_access(temp_root: Path):
    package = temp_root / "source" / "agent_core"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    shutil.copy2(ROOT / "source" / "agent_core" / "workspace_access.py", package)
    (package / "windows_security.py").write_text(
        """
from pathlib import Path
def acl_mode(profile_path): return 'off'
def default_profile_path(): return Path(__file__).resolve().parents[2] / 'localdata' / 'secure' / 'windows_security' / 'security_profile.json'
def load_security_profile(path): raise AssertionError('machine profile must not be required in ACL OFF')
""".strip(),
        encoding="utf-8",
    )
    sys.path.insert(0, str(temp_root / "source"))
    try:
        return importlib.import_module("agent_core.workspace_access")
    finally:
        sys.path.pop(0)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="smartagent-acl-off-policy-") as raw:
        temp_root = Path(raw)
        security = temp_root / "localdata" / "secure" / "windows_security"
        security.mkdir(parents=True)
        writable = temp_root / "writable"
        read_only = temp_root / "read-only"
        other = temp_root / "other"
        for path in (writable, read_only, other):
            path.mkdir()
        (security / "acl_mode.json").write_text(
            json.dumps({"schema": "SMARTAGENT_ACL_MODE_V1", "mode": "off"}),
            encoding="utf-8",
        )
        module = _load_workspace_access(temp_root)
        saved = module.save_workspace_access_policy([writable], [read_only])
        assert saved["schema"] == "SMARTAGENT_WORKSPACE_ACCESS_POLICY_V1"
        assert saved["revision"] == 1
        snapshot = module.access_snapshot()
        assert snapshot["writable_workspaces"] == (str(writable.resolve()),)
        assert snapshot["read_only_roots"] == (str(read_only.resolve()),)
        assert module.require_writable_workspace(writable) == str(writable.resolve())
        try:
            module.require_writable_workspace(other)
        except ValueError as exc:
            assert str(exc).startswith("workspace_not_in_authorized_registry:")
        else:
            raise AssertionError("ACL OFF must not bypass the writable registry")

    manager = (ROOT / "source" / "agent_core" / "workspace_manager.py").read_text(
        encoding="utf-8"
    )
    assert "def _apply_software_only_access" in manager
    assert "if _acl_off_enabled():\n            return _security_menu()" in manager
    acl_status = (ROOT / "install_smart_agent" / "acl_status.ps1").read_text(
        encoding="utf-8"
    )
    assert "SMARTAGENT_WORKSPACE_ACCESS_POLICY_V1" in acl_status
    assert "$accessPolicy = Get-WorkspaceAccessPolicy" in acl_status
    print("ACL_OFF_WORKSPACE_POLICY_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
