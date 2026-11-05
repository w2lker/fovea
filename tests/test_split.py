import json
import re
import unittest

from helpers import (HookCase, NEEDLE, git_log_output, grep_output, grep_single_file_output, json_array_output,
                     request_blocks)


class SplitTests(HookCase):
    def blocks_for(self, stdout, command):
        p = self.payload(stdout=stdout, command=command)
        r = self.run_hook(p)
        self.assertEqual(r.rc, 0)
        reqs = self.stub.requests
        self.assertEqual(len(reqs), 1)
        return request_blocks(reqs[0])

    def test_git_log_splits_at_commit_boundaries(self):
        blocks = self.blocks_for(git_log_output(60), "git log")
        self.assertEqual(len(blocks), 60)
        for _, text in blocks:
            self.assertRegex(text, r"^commit [0-9a-f]{7,}")
            self.assertEqual(len(re.findall(r"^commit [0-9a-f]{7,}", text, re.M)), 1)

    def test_git_log_after_cd_and_with_flags(self):
        blocks = self.blocks_for(git_log_output(40), "cd /tmp/repo && git log --stat -n 40")
        self.assertEqual(len(blocks), 40)
        for _, text in blocks:
            self.assertRegex(text, r"^commit [0-9a-f]{7,}")

    def test_grep_output_groups_by_file(self):
        blocks = self.blocks_for(grep_output(40, 3), "grep -rn compute src/")
        self.assertEqual(len(blocks), 40)
        for _, text in blocks:
            files = {l.split(":")[0] for l in text.splitlines() if l.strip()}
            self.assertEqual(len(files), 1, files)
        all_files = [next(iter({l.split(":")[0] for l in t.splitlines() if l.strip()})) for _, t in blocks]
        self.assertEqual(len(set(all_files)), 40)

    def test_rg_output_also_groups_by_file(self):
        blocks = self.blocks_for(grep_output(30, 3), "rg compute")
        self.assertEqual(len(blocks), 30)

    def test_path_line_shaped_output_from_unknown_command_groups_by_file(self):
        blocks = self.blocks_for(grep_output(30, 3), "./scripts/find-usages.sh compute")
        self.assertEqual(len(blocks), 30)

    def test_many_matches_in_one_file_grouped_by_20(self):
        blocks = self.blocks_for(grep_single_file_output(45), "grep -n value src/big.py")
        counts = [len([l for l in t.splitlines() if l.strip()]) for _, t in blocks]
        self.assertEqual(counts, [20, 20, 5])

    def test_json_array_splits_by_item(self):
        blocks = self.blocks_for(json_array_output(50), "curl -s https://api.example.com/items")
        self.assertEqual(len(blocks), 50)
        seen = []
        for _, text in blocks:
            toks = set(re.findall(r"ITEM-\d{3}", text))
            self.assertEqual(len(toks), 1, toks)
            seen.append(next(iter(toks)))
        self.assertEqual(seen, ["ITEM-%03d" % i for i in range(50)])

    def test_json_blocks_come_from_pretty_printed_copy(self):
        blocks = self.blocks_for(json_array_output(50), "curl -s https://api.example.com/items")
        pretty = json.dumps(json.loads(json_array_output(50)), indent=1)
        for _, text in blocks:
            self.assertIn(text.strip().strip(",").strip().splitlines()[0].strip(), pretty)
            self.assertGreater(len(text.splitlines()), 3)

    def test_paragraph_fallback_splits_on_blank_lines(self):
        from helpers import make_output
        blocks = self.blocks_for(make_output(30, needles=(10,)), "python3 report.py")
        self.assertEqual(len(blocks), 30)

    def test_forty_line_windows_when_no_paragraphs(self):
        out = "\n".join("row %04d %s" % (i, "k" * 60) for i in range(200))
        blocks = self.blocks_for(out, "python3 dump.py")
        self.assertGreaterEqual(len(blocks), 4)
        for _, text in blocks:
            self.assertLessEqual(len(text.splitlines()), 40)
            self.assertLessEqual(len(text), 2500 + 100)

    def test_small_blocks_merged_and_large_blocks_split(self):
        tiny = "\n\n".join("t%02d" % i for i in range(300))  # 300 tiny paragraphs
        big = "\n".join("big row %04d %s" % (i, "m" * 80) for i in range(80))  # one ~7k-char paragraph
        blocks = self.blocks_for(tiny + "\n\n" + big, "python3 dump.py")
        for _, text in blocks[:-1]:
            self.assertGreaterEqual(len(text), 200)
        for _, text in blocks:
            self.assertLessEqual(len(text), 2500 + 100)

    def test_unified_diff_by_content_regardless_of_command(self):
        secs = []
        for i in range(12):
            body = "\n".join("+    added line %d in file %d %s" % (j, i, "d" * 40) for j in range(6))
            secs.append("diff --git a/src/f%02d.py b/src/f%02d.py\nindex 1111111..2222222 100644\n"
                        "--- a/src/f%02d.py\n+++ b/src/f%02d.py\n@@ -1,3 +1,9 @@\n%s" % (i, i, i, i, body))
        blocks = self.blocks_for("\n".join(secs), "gh pr diff 12")
        self.assertEqual(len(blocks), 12)
        for _, text in blocks:
            self.assertRegex(text, r"^diff --git ")
            self.assertEqual(len(re.findall(r"^diff --git ", text, re.M)), 1)

    def test_commit_sections_by_content_for_non_git_command(self):
        blocks = self.blocks_for(git_log_output(40), "cat saved-log.txt")
        self.assertEqual(len(blocks), 40)
        for _, text in blocks:
            self.assertRegex(text, r"^commit [0-9a-f]{7,}")
            self.assertEqual(len(re.findall(r"^commit [0-9a-f]{7,}", text, re.M)), 1)

    def test_json_by_content_for_unrelated_command(self):
        blocks = self.blocks_for(json_array_output(50), "./fetch-items.sh")
        self.assertEqual(len(blocks), 50)


if __name__ == "__main__":
    unittest.main()
