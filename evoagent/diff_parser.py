# 作者：LHF
# 时间：2026-11-01
# 作用：解析统一差异并记录文件与新增代码行的位置。
import re
from dataclasses import dataclass
from typing import List

from .models import ChangedLine


@dataclass
class ParsedDiff:
    files: List[str]
    added_lines: List[ChangedLine]


HUNK = re.compile(r"^@@ -(?:\d+)(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


# 解析统一差异的文件路径、变更块及新增行号。
def parse_unified_diff(diff: str) -> ParsedDiff:
    files: List[str] = []
    added: List[ChangedLine] = []
    current_path = ""
    new_line = 0
    in_hunk = False

    for raw in diff.splitlines():
        # 目标文件头决定新增行所属路径；删除文件不计入目标文件列表。
        if raw.startswith("+++ "):
            current_path = raw[4:].strip()
            if current_path.startswith("b/"):
                current_path = current_path[2:]
            if current_path != "/dev/null" and current_path not in files:
                files.append(current_path)
            in_hunk = False
            continue
        match = HUNK.match(raw)
        if match:
            new_line = int(match.group(1))
            in_hunk = True
            continue
        if not in_hunk:
            continue
        # 新增行和上下文行推进目标行号，删除行不推进目标行号。
        if raw.startswith("+") and not raw.startswith("+++"):
            added.append(ChangedLine(current_path or "unknown", new_line, raw[1:]))
            new_line += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            continue
        elif raw.startswith("\\ No newline"):
            continue
        else:
            new_line += 1

    return ParsedDiff(files=files, added_lines=added)

