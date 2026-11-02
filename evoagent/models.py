# 作者：LHF
# 时间：2026-11-01
# 作用：定义任务、差异、审查结论、报告及轨迹的数据模型。
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class TaskState(str, Enum):
    PENDING = "PENDING"
    PLANNING = "PLANNING"
    EXECUTING = "EXECUTING"
    REVIEWING = "REVIEWING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ComponentKind(str, Enum):
    """按实际行为定义产品组件分类，名称不代表品牌。"""

    LLM_AGENT = "llm-agent"
    TOOL_SCANNER = "tool-scanner"
    GATE = "gate"


@dataclass
class ChangedLine:
    path: str
    line: int
    content: str


@dataclass
class Finding:
    rule_id: str
    severity: Severity
    title: str
    explanation: str
    path: str
    line: int
    evidence: str
    fix: str
    test: str
    confidence: float = 0.8
    evidence_refs: List[Dict[str, Any]] = field(default_factory=list)
    call_chain: List[Dict[str, Any]] = field(default_factory=list)
    source: str = "unknown"
    gate: Dict[str, Any] = field(default_factory=dict)
    # 此字段提供评测使用的规范化分类；rule_id 仍可作为内部标记
    # 或特定审查器标签，无需在不同模型之间保持稳定。
    cwe: Optional[str] = None

    # 将数据模型转换为可序列化字典。
    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["severity"] = self.severity.value
        return value


@dataclass
class ReviewReport:
    repository: str
    pull_request: Optional[int]
    summary: str
    risk: str
    findings: List[Finding] = field(default_factory=list)
    files_reviewed: List[str] = field(default_factory=list)
    reviewer: str = "local-rules"
    collaboration: Dict[str, Any] = field(default_factory=dict)
    run_mode: Dict[str, Any] = field(default_factory=dict)
    components: List[Dict[str, Any]] = field(default_factory=list)
    execution: Dict[str, Any] = field(default_factory=dict)

    # 将数据模型转换为可序列化字典。
    def to_dict(self) -> Dict[str, Any]:
        return {
            "repository": self.repository,
            "pull_request": self.pull_request,
            "summary": self.summary,
            "risk": self.risk,
            "findings": [item.to_dict() for item in self.findings],
            "files_reviewed": self.files_reviewed,
            "reviewer": self.reviewer,
            "collaboration": self.collaboration,
            "run_mode": self.run_mode,
            "components": self.components,
            "execution": self.execution,
        }


@dataclass
class TraceEvent:
    step: int
    state: TaskState
    message: str
    created_at: str

    # 将数据模型转换为可序列化字典。
    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["state"] = self.state.value
        return value
