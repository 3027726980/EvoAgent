# 作者：LHF
# 时间：2026-11-01
# 作用：验证统一差异的新增代码行号解析。
import unittest

from evoagent.diff_parser import parse_unified_diff


DIFF = """diff --git a/app.py b/app.py
index 123..456 100644
--- a/app.py
+++ b/app.py
@@ -2,3 +2,4 @@ def run():
 keep = True
-old = 1
+new = 2
+eval(user_input)
 tail = 3
"""


class DiffParserTests(unittest.TestCase):
    # 验证新增代码行号解析。
    def test_parses_added_line_numbers(self):
        parsed = parse_unified_diff(DIFF)
        self.assertEqual(["app.py"], parsed.files)
        self.assertEqual([(3, "new = 2"), (4, "eval(user_input)")], [(x.line, x.content) for x in parsed.added_lines])


if __name__ == "__main__":
    unittest.main()

