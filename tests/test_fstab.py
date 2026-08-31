import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autofstab.model import Entry, RawLine, parse_fstab, render_fstab
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


if __name__ == "__main__":
    unittest.main()
