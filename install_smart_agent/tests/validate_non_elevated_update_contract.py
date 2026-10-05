from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
BAT = ROOT / "update.bat"
PS1 = ROOT / "install_smart_agent" / "update.ps1"

class NonElevatedUpdateContractTests(unittest.TestCase):
    def test_normal_update_has_no_elevation_or_acl_transition(self):
        text = (BAT.read_text(encoding="utf-8") + "\n" + PS1.read_text(encoding="utf-8")).lower()
        forbidden = ("start-process", "-verb runas", "bootstrapprotectedupdater", "elevatedphase", "acl_status.ps1", "security_acl_policy.ps1", "grant-smartagentacllease")
        for token in forbidden:
            self.assertNotIn(token, text, f"normal update must not contain privileged token: {token}")

    def test_acl_gate_precedes_mutating_update_phases(self):
        text = PS1.read_text(encoding="utf-8")
        gate = text.index("Assert-AclOff $target")
        for marker in ("New-Object System.Threading.Mutex", "New-Item -ItemType Directory -Path $script:StageRoot", "Backup-TargetFiles $files $target", "Commit-Stage $files $script:StageRoot $target"):
            self.assertGreater(text.index(marker), gate, f"{marker} must remain after ACL OFF gate")

    def test_acl_gate_is_fail_closed(self):
        text = PS1.read_text(encoding="utf-8")
        for code in ("UPDATE_BLOCKED_ACL_STATE_MISSING", "UPDATE_BLOCKED_ACL_STATE_INVALID", "UPDATE_BLOCKED_ACL_FOREIGN_INSTALL", "UPDATE_BLOCKED_ACL_ON"):
            self.assertIn(code, text)

if __name__ == "__main__":
    unittest.main()