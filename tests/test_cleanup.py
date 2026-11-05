import os
import time
import unittest

from helpers import HookCase

DAY = 86400


class CleanupTests(HookCase):
    def mk(self, name, age_days=0.0, size=100, sub="out"):
        d = os.path.join(self.home, sub)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, name)
        with open(path, "wb") as f:
            f.write(b"x" * size)
        t = time.time() - age_days * DAY
        os.utime(path, (t, t))
        return path

    def test_old_files_removed_fresh_kept_default_retention(self):
        old = self.mk("deadbeef.txt", age_days=40)
        fresh = self.mk("cafebabe.txt", age_days=2)
        r = self.run_hook(self.payload())
        self.assertNotEqual(r.stdout, "")
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(fresh))
        self.assertTrue(os.path.exists(self.out_path()))

    def test_retention_days_is_configurable(self):
        old = self.mk("11111111.txt", age_days=5)
        fresh = self.mk("22222222.txt", age_days=1)
        self.run_hook(self.payload(), FOVEA_RETENTION_DAYS="3")
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(fresh))

    def test_cleanup_runs_in_shadow_mode_too(self):
        old = self.mk("33333333.txt", age_days=40)
        self.run_hook(self.payload(), FOVEA_MODE="shadow")
        self.assertFalse(os.path.exists(old))

    def test_cleanup_at_most_once_a_day(self):
        self.run_hook(self.payload(tool_use_id="toolu_first"))
        old = self.mk("44444444.txt", age_days=40)
        self.run_hook(self.payload(tool_use_id="toolu_second"))
        self.assertTrue(os.path.exists(old), "second run on the same day must not clean again")

    def test_store_size_cap_removes_oldest_first(self):
        paths = [self.mk("0000000%d.txt" % i, age_days=(4 - i) / 24.0, size=600 * 1024) for i in range(4)]
        # ages: 4h, 3h, 2h, 1h  (index 0 is the oldest)
        self.run_hook(self.payload(), FOVEA_MAX_STORE_MB="1")
        total = sum(os.path.getsize(os.path.join(self.home, "out", f))
                    for f in os.listdir(os.path.join(self.home, "out")))
        self.assertLessEqual(total, 1024 * 1024)
        self.assertFalse(os.path.exists(paths[0]))
        self.assertFalse(os.path.exists(paths[1]))
        self.assertTrue(os.path.exists(paths[3]))


if __name__ == "__main__":
    unittest.main()
