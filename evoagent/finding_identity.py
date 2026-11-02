# 作者：LHF
# 时间：2026-11-01
# 作用：统一审查与评测使用的问题标识和 CWE 分类。
"""审查与评测代码共用的规范化问题标识。"""

import re
from typing import Any


# 评测器比较规范化 CWE 标识，不依赖特定审查器的规则名。
RULE_TO_CWE = {
    "SEC-EVAL": "CWE-95",
    "SEC-SUBPROCESS-SHELL": "CWE-78",
    "SEC-HARDCODED-SECRET": "CWE-798",
    "SEC-SQL-CONCAT": "CWE-89",
    "REL-EMPTY-EXCEPT": "CWE-703",
    "REL-DEBUG-PRINT": "CWE-532",
    "SEC-PATH-TRAVERSAL": "CWE-22",
    "SEC-YAML-LOAD": "CWE-502",
    "SEC-WEAK-HASH": "CWE-328",
    "SEC-INSECURE-TEMPFILE": "CWE-377",
    "SEC-WEAK-RANDOM": "CWE-330",
    "REL-UNBOUNDED-RETRY": "CWE-835",
    "SEC-ASSERT-AUTH": "CWE-617",
    "SEC-INSECURE-COOKIE": "CWE-614",
    "SEC-PICKLE-LOAD": "CWE-502",
    "REL-FLOAT-MONEY": "CWE-682",
    "REL-NAIVE-DATETIME": "CWE-367",
    "REL-BLOCKING-ASYNC": "CWE-400",
    "REL-NONATOMIC-WRITE": "CWE-362",
    "SEC-OPEN-REDIRECT": "CWE-601",
    "SEC-LOG-FORGING": "CWE-117",
}

_CWE_RE = re.compile(r"^CWE-[0-9]+$", re.IGNORECASE)


def canonical_cwe(rule_id: Any = "", cwe: Any = "") -> str:
    """返回规范化 CWE，优先采用显式且有效的 CWE 字段。"""
    explicit = str(cwe or "").strip().upper()
    if _CWE_RE.fullmatch(explicit):
        return explicit
    rule = str(rule_id or "").strip().upper()
    mapped = RULE_TO_CWE.get(rule, "")
    return mapped.upper() if mapped else ""


def is_valid_cwe(value: Any) -> bool:
    """校验显式 CWE 值的传输格式。"""
    return bool(_CWE_RE.fullmatch(str(value or "").strip()))


def canonical_identity(rule_id: Any = "", cwe: Any = "") -> str:
    """返回评分标识，并兼容旧规则标识。

    已知规则及显式 CWE 按 CWE 评分；未知的旧规则标识只能与完全相同的标识匹配，
    任意模型生成的标识不能匹配带 CWE 标签的标准答案。"""
    value = canonical_cwe(rule_id, cwe)
    if value:
        return value
    rule = str(rule_id or "").strip().upper()
    return "RULE:" + rule if rule else ""
