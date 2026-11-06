"""WinError-5 Windows Job breakaway retry, scoped to RemoteAgent restart."""

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from RemoteAgent import remote_restart


class RestartBreakawayRegression(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="restart-flag-it-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.flags = (0x00000200, 0x00000008, 0x01000000)
        patchers = [
            patch.object(remote_restart, "_is_windows_runtime", return_value=True),
            patch.object(remote_restart.subprocess, "CREATE_NEW_PROCESS_GROUP", self.flags[0], create=True),
            patch.object(remote_restart.subprocess, "DETACHED_PROCESS", self.flags[1], create=True),
            patch.object(remote_restart.subprocess, "CREATE_BREAKAWAY_FROM_JOB", self.flags[2], create=True),
            patch.object(remote_restart, "_log"),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def spawn(self, popen):
        return remote_restart.spawn_restart_helper(
            root=self.root, old_pid=124, old_generation="G-1",
            chat_id=88, reply_to_message_id=99, popen=popen,
        )

    def test_breakaway_normal_path_single_spawn(self):
        fake = Mock(return_value=SimpleNamespace(pid=301))
        self.assertEqual(self.spawn(fake), 301)
        fake.assert_called_once()
        args, kwargs = fake.call_args
        self.assertEqual(kwargs["creationflags"], 0x01000208)
        self.assertEqual(kwargs["cwd"], str(self.root))
        self.assertFalse(kwargs["close_fds"])
        self.assertEqual(args[0][1:3], ["-m", "RemoteAgent.remote_restart"])
        self.assertEqual(args[0][3:5], ["--root", str(self.root)])
        remote_restart._log.assert_not_called()

    def test_winerror5_breakaway_denied_retries_once_without_breakaway(self):
        denial = PermissionError(13, "Access denied")
        denial.winerror = 5
        fake = Mock(side_effect=[denial, SimpleNamespace(pid=302)])
        self.assertEqual(self.spawn(fake), 302)
        self.assertEqual(fake.call_count, 2)
        first, second = fake.call_args_list
        self.assertEqual(first.kwargs["creationflags"], 0x01000208)
        self.assertEqual(second.kwargs["creationflags"], 0x00000208)
        self.assertEqual(first.args[0], second.args[0])
        self.assertEqual(first.kwargs["cwd"], second.kwargs["cwd"])
        self.assertIn("RESTART_HELPER_BREAKAWAY_DENIED", remote_restart._log.call_args_list[0].args[1])
        self.assertIn("FALLBACK_STARTED", remote_restart._log.call_args_list[1].args[1])

    def test_other_permission_error_not_retried(self):
        denial = PermissionError(13, "Access denied")
        denial.winerror = 32
        fake = Mock(side_effect=denial)
        with self.assertRaises(PermissionError):
            self.spawn(fake)
        fake.assert_called_once()
        remote_restart._log.assert_not_called()

    def test_second_denial_propagates_with_no_false_success(self):
        first = PermissionError(13, "Access denied")
        first.winerror = 5
        second = PermissionError(13, "Access denied")
        second.winerror = 5
        fake = Mock(side_effect=[first, second])
        with self.assertRaises(PermissionError):
            self.spawn(fake)
        self.assertEqual(fake.call_count, 2)
        self.assertEqual(remote_restart._log.call_count, 1)

    def test_unrelated_oserror_not_retried(self):
        fake = Mock(side_effect=OSError(2, "not found"))
        with self.assertRaises(OSError):
            self.spawn(fake)
        fake.assert_called_once()

    def test_posix_remains_single_spawn_without_windows_flags(self):
        with patch.object(remote_restart, "_is_windows_runtime", return_value=False):
            fake = Mock(return_value=SimpleNamespace(pid=399))
            self.assertEqual(self.spawn(fake), 399)
            fake.assert_called_once()
            self.assertEqual(fake.call_args.kwargs["creationflags"], 0)
            self.assertTrue(fake.call_args.kwargs["close_fds"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
