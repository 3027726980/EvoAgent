# 作者：LHF
# 时间：2026-11-01
# 作用：管理审查状态流转、节点检查点和失败恢复。
"""由 EvoAgent 自有运行时驱动、支持检查点的审查工作流。"""
import threading
from typing import Any, Dict, Optional, TypedDict

from .diff_parser import ParsedDiff, parse_unified_diff
from .models import ChangedLine, Finding, ReviewReport, Severity, TaskState, TraceEvent
from .reviewer import Reviewer
from .runtime import (
    AgentRuntime, RuntimeBudgetExceeded, RuntimeCancelled, RuntimeNode,
)
from .store import TaskStore, utc_now


ALLOWED = {
    TaskState.PENDING: {TaskState.PLANNING, TaskState.FAILED, TaskState.CANCELLED},
    TaskState.PLANNING: {TaskState.EXECUTING, TaskState.FAILED, TaskState.CANCELLED},
    TaskState.EXECUTING: {TaskState.REVIEWING, TaskState.FAILED, TaskState.CANCELLED},
    TaskState.REVIEWING: {TaskState.SUCCESS, TaskState.FAILED, TaskState.CANCELLED},
}


class RuntimeState(TypedDict, total=False):
    task_id: str
    repository: str
    pull_request: Optional[int]
    tenant_id: str
    diff: str
    parsed: Dict[str, Any]
    findings: list
    report: Dict[str, Any]


BudgetExceeded = RuntimeBudgetExceeded
TaskCancelled = RuntimeCancelled


class ReviewHarness:
    node_order = ("planning", "executing", "reviewing")

    # 组装审查器、持久化存储和带重试及预算的工作流运行时。
    def __init__(
        self, store: TaskStore, reviewer: Reviewer, max_steps: int = 8,
        timeout_seconds: int = 120, node_retries: int = 2, observability=None,
    ):
        self.store = store
        self.reviewer = reviewer
        self.max_steps = max_steps
        self.timeout_seconds = timeout_seconds
        self.node_retries = node_retries
        self.observability = observability
        self.name = "evoagent-runtime"
        self._ctx = threading.local()
        self.runtime = AgentRuntime(max_steps, timeout_seconds, node_retries)

    # 恢复任务并执行规划、审查和汇总，保存最终状态。
    def run(
        self, task_id: str, repository: str, pull_request: Optional[int], diff: str,
        tenant_id: str = "default",
    ) -> ReviewReport:
        task = self.store.get(task_id)
        # 成功任务直接返回已有报告，保证重复调用不会重新审查。
        if task and task.get("state") == TaskState.SUCCESS.value and task.get("report"):
            return self._report_from_dict(task["report"])
        state: RuntimeState = {
            "task_id": task_id, "repository": repository,
            "pull_request": pull_request, "diff": diff, "tenant_id": tenant_id,
        }
        self._ctx.step = max([item["step"] for item in (task or {}).get("trace", [])] or [0])
        self._ctx.task_id = task_id
        checkpoints = self.store.load_checkpoints(task_id)
        self._ctx.state = TaskState.PENDING
        if checkpoints.get("planning", {}).get("status") == "completed":
            self._ctx.state = TaskState.PLANNING
        if checkpoints.get("executing", {}).get("status") == "completed":
            self._ctx.state = TaskState.EXECUTING
        if checkpoints.get("reviewing", {}).get("status") == "completed":
            self._ctx.state = TaskState.REVIEWING
        try:
            result = self.runtime.execute(
                state,
                [
                    RuntimeNode("planning", self._planning),
                    RuntimeNode("executing", self._executing),
                    RuntimeNode("reviewing", self._reviewing),
                ],
                task_id=task_id, checkpoint_store=self.store,
                cancel_check=lambda: self.store.is_cancelled(task_id),
                span_factory=self._span,
            )
            report = self._report_from_dict(result["report"])
            self._ctx.step += 1
            self.store.succeed(
                task_id, report,
                TraceEvent(self._ctx.step, TaskState.SUCCESS, "Review completed", utc_now()),
            )
            return report
        # 取消状态单独持久化，避免被一般异常分支记录为失败。
        except TaskCancelled as exc:
            self._ctx.step += 1
            self.store.cancel(
                task_id, TraceEvent(self._ctx.step, TaskState.CANCELLED, str(exc), utc_now())
            )
            raise
        except Exception as exc:
            self._ctx.step += 1
            self.store.fail(
                task_id, str(exc),
                TraceEvent(self._ctx.step, TaskState.FAILED, "Review failed: %s" % exc, utc_now()),
            )
            try:
                self.store.record_failure_case(
                    task_id, "execution_error", {"error": str(exc)[:1000]}
                )
            except Exception:
                pass
            raise

    # 使用已有任务数据继续执行审查。
    def resume(
        self, task_id: str, repository: str, pull_request: Optional[int], diff: str,
        tenant_id: str = "default",
    ) -> ReviewReport:
        return self.run(task_id, repository, pull_request, diff, tenant_id)

    # 验证并解析差异，将规划结果保存到运行时状态。
    def _planning(self, state: RuntimeState) -> Dict[str, Any]:
        parsed = parse_unified_diff(state["diff"])
        if not parsed.files and not parsed.added_lines:
            raise ValueError("diff does not contain a valid unified diff with added lines")
        self._transition(TaskState.PLANNING, "Input accepted; preparing review plan")
        return {"parsed": self._serialize_parsed(parsed)}

    # 调用审查器并转换结论为可持久化状态。
    def _executing(self, state: RuntimeState) -> Dict[str, Any]:
        parsed = self._deserialize_parsed(state["parsed"])
        self._transition(
            TaskState.EXECUTING, "Reviewing %d changed files" % len(parsed.files)
        )
        contextual = getattr(self.reviewer, "review_with_context", None)
        findings = (
            contextual(
                state["task_id"], state["diff"], parsed,
                repository=state["repository"], tenant_id=state.get("tenant_id", "default"),
            )
            if contextual else self.reviewer.review(state["diff"], parsed)
        )
        return {"findings": [item.to_dict() for item in findings]}

    # 汇总结论、风险、协作与执行信息并生成报告。
    def _reviewing(self, state: RuntimeState) -> Dict[str, Any]:
        parsed = self._deserialize_parsed(state["parsed"])
        findings = [self._finding_from_dict(item) for item in state["findings"]]
        self._transition(
            TaskState.REVIEWING, "Validating and ranking %d findings" % len(findings)
        )
        risk = self._risk(findings)
        summary_reader = getattr(self.reviewer, "collaboration_summary", None)
        reviewer_summary = summary_reader(state["task_id"]) if summary_reader else {}
        if reviewer_summary and "run_mode" in reviewer_summary:
            collaboration = dict(reviewer_summary.get("collaboration") or {})
            run_mode = dict(reviewer_summary.get("run_mode") or {})
            components = list(reviewer_summary.get("components") or [])
            execution = dict(reviewer_summary.get("execution") or {})
            execution["gates"] = reviewer_summary.get("gates") or {}
            execution["rejected_findings"] = reviewer_summary.get("rejected_findings") or []
            execution["repository_context"] = reviewer_summary.get("repository_context") or {}
        else:
            collaboration = reviewer_summary or self._persisted_collaboration_summary(state["task_id"])
            run_mode, components, execution = {}, [], {}
        report = ReviewReport(
            repository=state["repository"], pull_request=state.get("pull_request"),
            summary=self._summary(findings, len(parsed.files), risk), risk=risk,
            findings=findings, files_reviewed=parsed.files, reviewer=self.reviewer.name,
            collaboration=collaboration,
            run_mode=run_mode, components=components, execution=execution,
        )
        return {"report": report.to_dict()}

    # 校验状态迁移并保存任务轨迹。
    def _transition(self, target: TaskState, message: str) -> None:
        if target == self._ctx.state:
            return
        if target not in ALLOWED.get(self._ctx.state, set()):
            raise RuntimeError(
                "invalid state transition: %s -> %s" % (self._ctx.state.value, target.value)
            )
        self._ctx.step += 1
        self._ctx.state = target
        self.store.transition(
            self._ctx.task_id,
            TraceEvent(self._ctx.step, target, message, utc_now()),
        )

    # 创建节点追踪范围，未启用时使用空上下文。
    def _span(self, name: str, attributes: Dict[str, Any]):
        if self.observability:
            return self.observability.span(
                name, str(attributes.get("task_id", "")), **attributes
            )
        from contextlib import nullcontext
        return nullcontext()

    # 将差异结构转换为可持久化字典。
    @staticmethod
    def _serialize_parsed(parsed: ParsedDiff) -> Dict[str, Any]:
        return {
            "files": parsed.files,
            "added_lines": [
                {"path": item.path, "line": item.line, "content": item.content}
                for item in parsed.added_lines
            ],
        }

    # 从检查点字典还原差异与新增代码行。
    @staticmethod
    def _deserialize_parsed(value: Dict[str, Any]) -> ParsedDiff:
        return ParsedDiff(
            list(value["files"]), [ChangedLine(**item) for item in value["added_lines"]]
        )

    # 还原问题模型及严重程度枚举。
    @staticmethod
    def _finding_from_dict(value: Dict[str, Any]) -> Finding:
        item = dict(value)
        item["severity"] = Severity(item["severity"])
        return Finding(**item)

    # 从字典恢复审查报告模型。
    @classmethod
    def _report_from_dict(cls, value: Dict[str, Any]) -> ReviewReport:
        return ReviewReport(
            repository=value["repository"], pull_request=value.get("pull_request"),
            summary=value["summary"], risk=value["risk"],
            findings=[cls._finding_from_dict(item) for item in value.get("findings", [])],
            files_reviewed=list(value.get("files_reviewed", [])),
            reviewer=value.get("reviewer", "unknown"),
            collaboration=dict(value.get("collaboration", {})),
            run_mode=dict(value.get("run_mode", {})),
            components=list(value.get("components", [])),
            execution=dict(value.get("execution", {})),
        )

    # 根据结论严重程度计算风险等级。
    @staticmethod
    def _risk(findings) -> str:
        severities = {item.severity for item in findings}
        if Severity.CRITICAL in severities or Severity.HIGH in severities:
            return "high"
        if Severity.MEDIUM in severities:
            return "medium"
        return "low"

    # 按文件数、问题数及风险等级生成摘要。
    @staticmethod
    def _summary(findings, file_count: int, risk: str) -> str:
        if not findings:
            return "Reviewed %d file(s); no actionable issue was detected in added lines." % file_count
        return "Reviewed %d file(s); found %d actionable issue(s). Overall risk: %s." % (
            file_count, len(findings), risk,
        )

    # 从保存的角色消息还原协作统计。
    def _persisted_collaboration_summary(self, task_id: str) -> Dict[str, Any]:
        task = self.store.get(task_id) or {}
        messages = task.get("collaboration", [])
        if not messages:
            return {}
        kinds = [item.get("kind", "") for item in messages]
        roles = sorted({
            value for item in messages
            for value in (item.get("sender", ""), item.get("recipient", ""))
            if value and value not in {"all", "review-report"}
        })
        rounds = [
            int((item.get("content") or {}).get("round", 0))
            for item in messages
            if isinstance(item.get("content"), dict)
        ]
        final = next((
            item.get("content") or {} for item in reversed(messages)
            if item.get("kind") == "arbitration_decision"
        ), {})
        return {
            "protocol": "plan-challenge-revise-evidence-verify-arbitrate",
            "roles": roles,
            "planned_assignments": kinds.count("assignment"),
            "dialogue_rounds": max(rounds or [1]),
            "messages": len(messages),
            "retries": kinds.count("retry_request"),
            "handoffs": kinds.count("assignment_handoff"),
            "approved_findings": len(final.get("approved_findings", [])),
            "rejected_findings": len(final.get("rejected_findings", [])),
        }
