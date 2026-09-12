import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autofstab.model import (
    Entry, RawLine, automount_mountpoints, mountpoints_to_create, parse_fstab,
    read_records, render_fstab,
)
from autofstab.devices import _invoking_user_ids
from autofstab.validate import validate_entries

SAMPLE = """\
# /etc/fstab: static file system information
#
UUID=1111-2222  /       ext4  defaults        0  1
UUID=3333-4444  /home   ext4  defaults,nofail 0  2

# swap
UUID=5555-6666  none    swap  sw              0  0
"""


class TestModel(unittest.TestCase):
    def setUp(self):
        self.path = "/tmp/autofstab_test_sample.fstab"
        with open(self.path, "w") as f:
            f.write(SAMPLE)

    def tearDown(self):
        os.unlink(self.path)

    def test_parse_counts_entries_and_preserves_comments(self):
        records = parse_fstab(self.path)
        entries = [r for r in records if isinstance(r, Entry)]
        comments = [r for r in records if isinstance(r, RawLine) and r.text.strip().startswith("#")]
        self.assertEqual(len(entries), 3)
        self.assertEqual(len(comments), 3)

    def test_roundtrip_preserves_entry_data(self):
        records = parse_fstab(self.path)
        rendered = render_fstab(records)
        reparsed = [r for r in parse_fstab_from_text(rendered) if isinstance(r, Entry)]
        self.assertEqual(reparsed[0].device, "UUID=1111-2222")
        self.assertEqual(reparsed[0].mountpoint, "/")
        self.assertEqual(reparsed[1].options, "defaults,nofail")
        self.assertEqual(reparsed[2].fstype, "swap")

    def test_add_and_remove_entry(self):
        records = parse_fstab(self.path)
        records.append(Entry("UUID=7777-8888", "/data", "ext4", "defaults", 0, 2))
        self.assertEqual(len([r for r in records if isinstance(r, Entry)]), 4)
        del records[[i for i, r in enumerate(records) if isinstance(r, Entry)][-1]]
        self.assertEqual(len([r for r in records if isinstance(r, Entry)]), 3)


class TestValidate(unittest.TestCase):
    def test_duplicate_mountpoint_is_error(self):
        entries = [
            Entry("UUID=1", "/mnt", "ext4", "defaults", 0, 2),
            Entry("UUID=2", "/mnt", "ext4", "defaults", 0, 2),
        ]
        errors, _ = validate_entries(entries)
        self.assertTrue(any("duplicate mount point" in e for e in errors))

    def test_raw_device_path_warns(self):
        entries = [Entry("/dev/sdb1", "/mnt", "ext4", "defaults", 0, 2)]
        _, warnings = validate_entries(entries)
        self.assertTrue(any("UUID=" in w for w in warnings))

    def test_empty_fields_are_errors(self):
        entries = [Entry("", "/mnt", "", "defaults", 0, 2)]
        errors, _ = validate_entries(entries)
        self.assertTrue(any("device/source" in e for e in errors))
        self.assertTrue(any("filesystem type" in e for e in errors))

    def test_clean_entries_have_no_issues(self):
        entries = [Entry("UUID=1", "/", "ext4", "defaults", 0, 1)]
        errors, warnings = validate_entries(entries)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_duplicate_device_without_subvol_warns(self):
        entries = [
            Entry("UUID=1", "/data1", "ext4", "defaults", 0, 2),
            Entry("UUID=1", "/data2", "ext4", "defaults", 0, 2),
        ]
        _, warnings = validate_entries(entries)
        self.assertTrue(any("already used by entry" in w for w in warnings))

    def test_duplicate_device_with_subvol_does_not_warn(self):
        entries = [
            Entry("UUID=1", "/", "btrfs", "subvol=/@,defaults", 0, 1),
            Entry("UUID=1", "/home", "btrfs", "subvol=/@home,defaults", 0, 2),
        ]
        _, warnings = validate_entries(entries)
        self.assertFalse(any("already used by entry" in w for w in warnings))

    def test_missing_mountpoint_wording_depends_on_existing(self):
        pending = Entry("UUID=1", "/mnt/NewDrive", "ext4", "defaults", 0, 2, existing=False)
        existing = Entry("UUID=2", "/mnt/OldDrive", "ext4", "defaults", 0, 2, existing=True)
        _, warnings = validate_entries([pending, existing])
        pending_warning = next(w for w in warnings if "NewDrive" in w)
        existing_warning = next(w for w in warnings if "OldDrive" in w)
        self.assertIn("expected for a new entry", pending_warning)
        self.assertNotIn("expected for a new entry", existing_warning)


def parse_fstab_from_text(text):
    path = "/tmp/autofstab_test_roundtrip.fstab"
    with open(path, "w") as f:
        f.write(text)
    try:
        return parse_fstab(path)
    finally:
        os.unlink(path)



class TestMountpointHelpers(unittest.TestCase):
    """These are shared by both front ends, so a regression here would hit
    the GUI's save step and the CLI's alike."""

    def test_missing_mountpoint_is_queued(self):
        entry = Entry("UUID=x", "/definitely/not/here", "ext4", "defaults", 0, 2)
        self.assertEqual(mountpoints_to_create([entry]), ["/definitely/not/here"])

    def test_existing_mountpoint_is_not_queued(self):
        entry = Entry("UUID=x", "/tmp", "ext4", "defaults", 0, 2)
        self.assertEqual(mountpoints_to_create([entry]), [])

    def test_swap_never_gets_a_directory(self):
        by_mountpoint = Entry("UUID=x", "none", "swap", "sw", 0, 0)
        by_fstype = Entry("UUID=y", "/nope", "swap", "sw", 0, 0)
        self.assertEqual(mountpoints_to_create([by_mountpoint, by_fstype]), [])

    def test_duplicate_mountpoints_queued_once(self):
        a = Entry("UUID=x", "/definitely/not/here", "ext4", "defaults", 0, 2)
        b = Entry("UUID=y", "/definitely/not/here", "ext4", "defaults", 0, 2)
        self.assertEqual(mountpoints_to_create([a, b]), ["/definitely/not/here"])

    def test_automount_entry_is_detected(self):
        share = Entry("//nas/media", "/mnt/media", "cifs",
                      "noauto,x-systemd.automount,_netdev", 0, 0)
        self.assertEqual(automount_mountpoints([share]), ["/mnt/media"])

    def test_plain_noauto_is_not_an_automount(self):
        entry = Entry("UUID=x", "/mnt/thing", "ext4", "noauto", 0, 2)
        self.assertEqual(automount_mountpoints([entry]), [])

    def test_substring_option_does_not_count(self):
        """'x-systemd.automount-ish' must not match x-systemd.automount."""
        entry = Entry("UUID=x", "/mnt/thing", "ext4", "x-systemd.automounted", 0, 2)
        self.assertEqual(automount_mountpoints([entry]), [])


class TestInvokingUser(unittest.TestCase):
    """Getting this wrong hands every NTFS drive and SMB share to root."""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ("SUDO_UID", "SUDO_GID", "PKEXEC_UID")}
        for k in self._saved:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_sudo_env_wins(self):
        os.environ["SUDO_UID"], os.environ["SUDO_GID"] = "1000", "1000"
        self.assertEqual(_invoking_user_ids(), ("1000", "1000"))

    def test_pkexec_uid_is_honoured(self):
        """pkexec sets PKEXEC_UID, not SUDO_UID -- without this the fallback
        returns root's 0/0 and the share ends up root-owned."""
        os.environ["PKEXEC_UID"] = "0"
        self.assertEqual(_invoking_user_ids(), ("0", str(pwd.getpwuid(0).pw_gid)))

    def test_pkexec_uid_gid_comes_from_passwd(self):
        target = next((u for u in pwd.getpwall() if u.pw_uid != u.pw_gid and u.pw_uid > 0), None)
        if target is None:
            self.skipTest("no account with uid != gid on this system")
        os.environ["PKEXEC_UID"] = str(target.pw_uid)
        self.assertEqual(_invoking_user_ids(), (str(target.pw_uid), str(target.pw_gid)))

    def test_unknown_pkexec_uid_falls_back_without_crashing(self):
        os.environ["PKEXEC_UID"] = "4242424"
        self.assertEqual(_invoking_user_ids(), ("4242424", "4242424"))

    def test_garbage_pkexec_uid_does_not_raise(self):
        os.environ["PKEXEC_UID"] = "not-a-number"
        self.assertEqual(_invoking_user_ids(), ("not-a-number", "not-a-number"))


class TestReadRecords(unittest.TestCase):
    """An unreadable file must never be mistaken for an empty one: both front
    ends can escalate to root, so a later save would overwrite contents that
    were never visible."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, True)

    def test_readable_file_parses(self):
        path = os.path.join(self.tmpdir, "fstab")
        with open(path, "w") as f:
            f.write(SAMPLE)
        records, error = read_records(path)
        self.assertIsNone(error)
        self.assertTrue(records)

    def test_missing_file_is_empty_not_an_error(self):
        records, error = read_records(os.path.join(self.tmpdir, "absent"))
        self.assertEqual(records, [])
        self.assertIsNone(error)

    def test_unreadable_file_is_an_error_not_empty(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores the mode bits")
        path = os.path.join(self.tmpdir, "locked")
        with open(path, "w") as f:
            f.write(SAMPLE)
        os.chmod(path, 0o000)
        records, error = read_records(path)
        self.assertIsNone(records)
        self.assertTrue(error)

    def test_unreadable_directory_is_an_error_not_empty(self):
        """The nastier half: os.path.exists() returns False here, which is how
        this used to read as 'the file is empty' rather than as a failure."""
        if os.geteuid() == 0:
            self.skipTest("root ignores the mode bits")
        locked_dir = os.path.join(self.tmpdir, "locked_dir")
        os.mkdir(locked_dir)
        path = os.path.join(locked_dir, "fstab")
        with open(path, "w") as f:
            f.write(SAMPLE)
        os.chmod(locked_dir, 0o000)
        self.addCleanup(os.chmod, locked_dir, 0o755)
        self.assertFalse(os.path.exists(path))   # the trap
        records, error = read_records(path)
        self.assertIsNone(records)
        self.assertTrue(error)


class TestCliEndOfInput(unittest.TestCase):
    """Ctrl-D / Ctrl-C / the end of piped input must not produce a traceback."""

    ENTRY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "autofstab.py")

    def _run(self, stdin_text):
        with tempfile.NamedTemporaryFile("w", suffix="-fstab", delete=False) as f:
            f.write("# scratch\n")
            path = f.name
        self.addCleanup(os.unlink, path)
        return subprocess.run(
            [sys.executable, self.ENTRY, "--file", path],
            input=stdin_text, capture_output=True, text=True, timeout=60,
        )

    def test_eof_at_the_menu_is_clean(self):
        result = self._run("")
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        self.assertNotIn("EOFError", result.stdout + result.stderr)
        self.assertIn("Input ended", result.stdout)
        self.assertEqual(result.returncode, 1)

    def test_eof_midway_through_a_prompt_is_clean(self):
        # Stops partway into the manual add flow, so EOF lands on prompt()
        # rather than on the menu's own input().
        result = self._run("2\n2\ntmpfs\n")
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        self.assertIn("Input ended", result.stdout)
        self.assertEqual(result.returncode, 1)

    def test_explicit_quit_still_succeeds(self):
        result = self._run("9\n")
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()