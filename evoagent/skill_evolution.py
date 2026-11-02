# 作者：LHF
# 时间：2026-11-01
# 作用：管理标准 Agent Skill 包的候选生成、回放、激活及回滚。
"""通过回放门禁管理标准 Agent Skill 的 SKILL.md 包演进。"""
import hashlib
import json
import re
import threading
import uuid
from typing import Any, Callable, Dict, Optional

from .diff_parser import parse_unified_diff
from .evolution import RegressionEvaluator
from .reviewer import Reviewer
from .skills import AgentSkill, SKILL_NAME
from .store import utc_now


ARTIFACT_SCHEMA_VERSION = 2
RULE_ID = re.compile(r"[A-Z][A-Z0-9_-]{1,79}")


# 以稳定字段顺序生成规范化 JSON 文本。
def _canonical_json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# 对规范化内容生成 SHA-256 摘要。
def _sha256(value: dict) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


# 构造标准 Skill 初始元数据和正文。
def _default_skill_markdown(name: str) -> str:
    return """---
name: %s
description: Apply replay-validated review guidance learned from confirmed feedback. Use during code review when project-specific failure patterns may apply.
---

# Review project-specific failure patterns

Inspect the current change using repository evidence. Report only actionable defects introduced by added lines, and include a concrete fix and regression test.
""" % name


# 迁移旧产物为标准 SKILL.md 包。
def _migrate_legacy_artifact(artifact: dict, expected_name: str) -> dict:
    name = str(artifact.get("name", expected_name)).strip().lower()
    description = str(artifact.get("description") or (
        "Apply migrated project-specific guidance during code review."
    )).replace("\n", " ").strip()
    lines = [
        "---", "name: %s" % name,
        "description: %s" % json.dumps(description, ensure_ascii=False), "---", "",
        "# Migrated review guidance", "",
        "Apply these checks to added production lines and verify repository context.", "",
    ]
    for rule in artifact.get("rules") or []:
        rule_id = str(rule.get("rule_id", "REVIEW")).strip().upper()
        match = str(rule.get("match", "")).replace("`", "'").strip()
        if not match:
            continue
        lines.extend([
            "<!-- evoagent:learned:%s:start -->" % rule_id,
            "## %s" % rule_id, "",
            "Inspect added behavior equivalent to `%s`." % match,
            "Report `%s` at `%s` severity only when context confirms the defect."
            % (rule_id, str(rule.get("severity", "medium"))),
            "Cite the changed line, explain impact, propose a fix, and require a test.",
            "<!-- evoagent:learned:%s:end -->" % rule_id, "",
        ])
    return {"name": name, "files": {"SKILL.md": "\n".join(lines).strip() + "\n"}}


def validate_artifact(artifact: Any, expected_name: str = "") -> dict:
    """校验并规范化一个带版本的 Agent Skill 包。"""
    expected_name = str(expected_name).strip().lower()
    if expected_name and not SKILL_NAME.fullmatch(expected_name):
        raise ValueError("invalid Agent Skill name")
    if isinstance(artifact, str):
        artifact = {"name": expected_name, "files": {"SKILL.md": artifact}}
    if not isinstance(artifact, dict):
        raise ValueError("agent skill artifact must be an object")
    if "rules" in artifact and "files" not in artifact and "skill_md" not in artifact:
        artifact = _migrate_legacy_artifact(artifact, expected_name)
    if "skill_md" in artifact and "files" not in artifact:
        artifact = {
            **artifact,
            "files": {
                "SKILL.md": artifact.get("skill_md"),
                **dict(artifact.get("supporting_files") or {}),
            },
        }
    name = str(artifact.get("name", expected_name)).strip().lower()
    if expected_name and name != expected_name:
        raise ValueError("agent skill artifact name must match skill_name")
    skill = AgentSkill.from_artifact({
        "name": name, "files": dict(artifact.get("files") or {}),
    })
    normalized = skill.to_artifact()
    normalized["schema_version"] = ARTIFACT_SCHEMA_VERSION
    return normalized


class _ReplayTaskStore:
    # 保存 Skill 回放所需的最小任务输入。
    def __init__(self, skill_name: str):
        self.skill_name = skill_name

    # 读取任务并按需检查租户归属。
    def get(self, _task_id: str, _tenant_id: Optional[str] = None) -> dict:
        return {"input": {
            "mode": "agentic",
            "enabled_agents": ["lead", "security", "correctness-reliability", "critic"],
            "enabled_skills": [self.skill_name],
        }}


class AgentSkillReplayReviewer(Reviewer):
    """通过产品的 Lead 与工作角色 Skill 运行时回放候选。"""

    # 校验候选 Skill 并配置通过产品角色运行时回放的审查器。
    def __init__(self, artifact: dict, client, token_budget: int = 8000, time_budget_seconds: int = 60):
        from .agentic_core import AgenticReviewer

        self.skill = AgentSkill.from_artifact(artifact)
        self.name = "%s-agent-skill-replay" % self.skill.name
        self._sequence = 0
        self._last_task_id = ""
        self.token_budget = int(token_budget)
        self.time_budget_seconds = int(time_budget_seconds)
        self.agentic = AgenticReviewer(
            _ReplayTaskStore(self.skill.name), client,
            default_token_budget=token_budget,
            default_time_budget=time_budget_seconds,
            skill_provider=lambda _tenant: [self.skill],
        )

    # 审查差异并返回当前审查器识别的问题。
    def review(self, diff: str, parsed) -> list:
        return self.review_case({"diff": diff, "repository": ""}, parsed)

    # 以案例中的任务与仓库信息执行审查。
    def review_case(self, case: dict, parsed) -> list:
        self._sequence += 1
        self._last_task_id = "skill-replay:%s:%d" % (self.skill.name, self._sequence)
        return self.agentic.review_with_context(
            self._last_task_id, case["diff"], parsed,
            repository=str(case.get("repository_root") or case.get("repository") or ""),
        )

    # 返回最近一次评测的模型与工具统计。
    def evaluation_execution(self) -> dict:
        return dict(
            self.agentic.collaboration_summary(self._last_task_id).get("execution") or {}
        )

    # 返回最近一次评测的角色协作记录。
    def evaluation_collaboration(self) -> dict:
        return dict(
            self.agentic.collaboration_summary(self._last_task_id).get("collaboration") or {}
        )

    # 公开当前实验组的模型、规则与角色配置。
    def evaluation_config(self) -> dict:
        return {
            "mode": "agentic", "roles": [
                "lead", "security", "correctness-reliability", "critic",
            ],
            "skill": self.skill.name,
            "per_role_token_budget": self.token_budget,
            "per_role_time_budget_seconds": self.time_budget_seconds,
        }


class SkillEvolutionEngine:
    """创建、回放、激活及回滚 Agent Skill 包版本。"""

    # 配置 Skill 产物存储、回放工厂及激活门禁。
    def __init__(
        self, store, reviewer_factory: Optional[Callable[[dict], Reviewer]] = None,
        min_cases: int = 3, max_cases: int = 100, min_improvement: float = .01,
        min_holdout_cases: int = 2, max_metric_regression: float = 0.0,
    ):
        self.store = store
        self.reviewer_factory = reviewer_factory
        self.min_cases = min_cases
        self.max_cases = max_cases
        self.min_improvement = min_improvement
        self.min_holdout_cases = min_holdout_cases
        self.max_metric_regression = max_metric_regression
        self._lock = threading.RLock()

    # 创建不含学习内容的 Skill 产物。
    @staticmethod
    def empty_artifact(skill_name: str) -> dict:
        return validate_artifact(_default_skill_markdown(skill_name), skill_name)

    @classmethod
    def build_candidate_artifact(
        cls, skill_name: str, base_artifact: dict, feedback: list,
    ) -> dict:
        """为受控实验和生产实验执行纯产物变换。

        反馈必须已包含具体证据；此处不查询任务存储，
        以保持各实验组相互隔离且可复现。"""
        artifact = validate_artifact(base_artifact, skill_name)
        content = artifact["files"]["SKILL.md"]
        learned, removed, used = [], [], []
        for index, case in enumerate(feedback):
            finding = (case.get("payload") or {}).get("finding") or {}
            rule_id = str(finding.get("rule_id", "")).strip().upper()
            if not RULE_ID.fullmatch(rule_id):
                continue
            category = str(case.get("category", ""))
            if category == "false_positive":
                updated = cls._remove_learned_block(content, rule_id)
                if updated != content:
                    content = updated
                    removed.append(rule_id)
                    used.append(case.get("id", index))
                continue
            if category != "missed_issue":
                continue
            evidence = str(finding.get("evidence", "")).strip()
            if not evidence or len(evidence) > 240 or "\n" in evidence or "\r" in evidence:
                continue
            if "evoagent:learned:%s:start" % rule_id in content:
                continue
            content = content.rstrip() + "\n\n" + cls._learned_block(
                rule_id, finding, evidence,
            )
            learned.append(rule_id)
            used.append(case.get("id", index))
        candidate = validate_artifact({
            "name": skill_name,
            "files": {**artifact["files"], "SKILL.md": content},
        }, skill_name)
        return {
            "artifact": candidate,
            "learned_rule_ids": sorted(set(learned)),
            "removed_rule_ids": sorted(set(removed)),
            "used_feedback_ids": used,
        }

    # 使用已配置工厂创建回放审查器。
    def _factory(self, serialized: str) -> Reviewer:
        if self.reviewer_factory is None:
            raise RuntimeError("Agent Skill replay requires a configured model")
        return self.reviewer_factory(json.loads(serialized))

    # 隐藏保留集案例，仅公开汇总。
    @staticmethod
    def _redact_holdout(metrics: dict) -> dict:
        return {key: value for key, value in metrics.items() if key not in {"case_results", "errors"}}

    # 逐项校验受保护指标的非回退约束。
    def metrics_non_regressing(self, candidate: dict, baseline: dict) -> bool:
        protected = ["score", "precision", "recall", "high_severity_recall", "success_rate"]
        if baseline.get("positive_cases", 0):
            protected.append("severity_accuracy")
        if baseline.get("clean_cases", 0):
            protected.append("clean_accuracy")
        return all(
            float(candidate.get(key, 0)) + self.max_metric_regression
            >= float(baseline.get(key, 0)) for key in protected
        )

    # 检查受保护指标是否处于允许回退范围内。
    def _non_regressing(self, candidate: dict, baseline: dict) -> bool:
        return self.metrics_non_regressing(candidate, baseline)

    # 返回生效版本、样本数量与门禁状态。
    def status(self, skill_name: str = "evolved-review", tenant_id: str = "default") -> dict:
        validation = self.store.list_evaluation_cases("validation", True, self.max_cases)
        holdout = self.store.list_evaluation_cases("holdout", True, self.max_cases)
        active = self.store.get_active_skill_artifact(skill_name, tenant_id)
        return {
            "tenant_id": tenant_id, "skill_name": skill_name, "format": "agent-skill",
            "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
            "active_version": active.get("version") if active else None,
            "active_artifact_sha256": active.get("artifact_sha256") if active else None,
            "validation_cases": len(validation), "holdout_cases": len(holdout),
            "minimum_cases": self.min_cases, "minimum_holdout_cases": self.min_holdout_cases,
            "minimum_improvement": self.min_improvement,
            "maximum_metric_regression": self.max_metric_regression,
            "model_configured": self.reviewer_factory is not None,
            "ready": self.reviewer_factory is not None
            and len(validation) >= self.min_cases and len(holdout) >= self.min_holdout_cases,
        }

    # 在锁保护下提交候选并执行回放准入判断。
    def propose(self, skill_name: str, artifact: Any, tenant_id: str = "default") -> dict:
        skill_name = skill_name.strip().lower()
        candidate = validate_artifact(artifact, skill_name)
        with self._lock:
            return self._propose(skill_name, candidate, tenant_id)

    # 比较验证集及保留集指标并记录激活决策。
    def _propose(self, skill_name: str, artifact: dict, tenant_id: str) -> dict:
        active = self.store.get_active_skill_artifact(skill_name, tenant_id)
        baseline_artifact = (
            validate_artifact(active["artifact"], skill_name)
            if active else self.empty_artifact(skill_name)
        )
        if active and _sha256(baseline_artifact) == _sha256(artifact):
            return {
                "version": self._public_version(active), "decision": "deferred",
                "reason": "candidate Agent Skill is identical to the active version",
                "candidate": {}, "baseline": {}, "candidate_holdout": {},
                "baseline_holdout": {}, "gates": {}, "run_id": None,
            }
        validation = self.store.list_evaluation_cases("validation", True, self.max_cases)
        holdout = self.store.list_evaluation_cases("holdout", True, self.max_cases)
        # 验证集用于衡量候选提升，独立保留集用于防止回退。
        baseline_metrics = self._empty_metrics(len(validation))
        candidate_metrics = self._empty_metrics(len(validation))
        baseline_holdout = self._empty_metrics(len(holdout))
        candidate_holdout = self._empty_metrics(len(holdout))
        decision, reason = "deferred", ""
        gates = {
            "artifact_valid": True, "runtime_is_agent_skill": True,
            "model_configured": self.reviewer_factory is not None,
            "validation_dataset_ready": len(validation) >= self.min_cases,
            "holdout_dataset_ready": len(holdout) >= self.min_holdout_cases,
            "evaluation_success": None, "validation_improvement": None,
            "validation_non_regression": None, "holdout_non_regression": None,
        }
        if self.reviewer_factory is None:
            reason = "candidate saved but no model-backed Agent Skill replay is configured"
        elif len(validation) < self.min_cases:
            reason = "candidate saved but the validation dataset is smaller than the activation minimum"
        elif len(holdout) < self.min_holdout_cases:
            reason = "candidate saved but the holdout dataset is smaller than the activation minimum"
        else:
            evaluator = RegressionEvaluator(self._factory)
            baseline_metrics = evaluator.run(_canonical_json(baseline_artifact), validation)
            candidate_metrics = evaluator.run(_canonical_json(artifact), validation)
            baseline_holdout = evaluator.run(_canonical_json(baseline_artifact), holdout)
            candidate_holdout = evaluator.run(_canonical_json(artifact), holdout)
            no_errors = not (
                baseline_metrics["errors"] or candidate_metrics["errors"]
                or baseline_holdout["errors"] or candidate_holdout["errors"]
            )
            improved = candidate_metrics["score"] >= baseline_metrics["score"] + self.min_improvement
            validation_safe = self._non_regressing(candidate_metrics, baseline_metrics)
            holdout_safe = self._non_regressing(candidate_holdout, baseline_holdout)
            gates.update({
                "evaluation_success": no_errors, "validation_improvement": improved,
                "validation_non_regression": validation_safe,
                "holdout_non_regression": holdout_safe,
            })
            if no_errors and improved and validation_safe and holdout_safe:
                decision = "activated"
                reason = "candidate SKILL.md improved validation and passed holdout non-regression"
            else:
                decision = "rejected"
                failures = []
                if not no_errors:
                    failures.append("evaluation failed")
                if not improved:
                    failures.append("validation improvement was below threshold")
                if not validation_safe:
                    failures.append("a protected validation metric regressed")
                if not holdout_safe:
                    failures.append("a protected holdout metric regressed")
                reason = "; ".join(failures)
        # 所有候选都保留版本记录，仅通过门禁的候选被标记为生效。
        version = self.store.save_skill_artifact(
            skill_name, artifact, candidate_metrics.get("score", 0.0),
            decision == "activated", tenant_id,
        )
        candidate_change = self._skill_diff(baseline_artifact, artifact)
        run = {
            "id": str(uuid.uuid4()), "tenant_id": tenant_id, "skill_name": skill_name,
            "candidate_version": version["version"],
            "baseline_version": active.get("version") if active else None,
            "decision": decision, "candidate_score": candidate_metrics.get("score", 0.0),
            "baseline_score": baseline_metrics.get("score", 0.0), "created_at": utc_now(),
            "metrics": {
                "candidate": candidate_metrics, "baseline": baseline_metrics,
                "candidate_holdout": self._redact_holdout(candidate_holdout),
                "baseline_holdout": self._redact_holdout(baseline_holdout),
                "gates": gates, "reason": reason, "candidate_change": candidate_change,
                "reproducibility": {
                    "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
                    "candidate_artifact_sha256": version["artifact_sha256"],
                    "baseline_artifact_sha256": active.get("artifact_sha256")
                    if active else _sha256(baseline_artifact),
                },
            },
        }
        self.store.save_skill_evolution_run(run)
        return {
            "version": version, "decision": decision, "reason": reason,
            "candidate": candidate_metrics, "baseline": baseline_metrics,
            "candidate_holdout": self._redact_holdout(candidate_holdout),
            "baseline_holdout": self._redact_holdout(baseline_holdout),
            "gates": gates, "run_id": run["id"], "candidate_change": candidate_change,
        }

    # 从有效反馈构造候选，去重后执行回放门禁。
    def auto_propose(
        self, skill_name: str = "evolved-review", tenant_id: Optional[str] = None,
    ) -> dict:
        skill_name = skill_name.strip().lower()
        if not SKILL_NAME.fullmatch(skill_name):
            raise ValueError("invalid Agent Skill name")
        failures = self.store.list_failure_cases(True, 100, tenant_id)
        tenant_id = tenant_id or "default"
        active = self.store.get_active_skill_artifact(skill_name, tenant_id)
        artifact = (
            validate_artifact(active["artifact"], skill_name)
            if active else self.empty_artifact(skill_name)
        )
        content = artifact["files"]["SKILL.md"]
        used_ids, learned, removed = [], [], []
        for case in failures:
            finding = (case.get("payload") or {}).get("finding") or {}
            rule_id = str(finding.get("rule_id", "")).strip().upper()
            if not RULE_ID.fullmatch(rule_id):
                continue
            if case.get("category") == "false_positive":
                updated = self._remove_learned_block(content, rule_id)
                if updated != content:
                    content = updated
                    removed.append(rule_id)
                    used_ids.append(case["id"])
                continue
            if case.get("category") != "missed_issue":
                continue
            evidence = str(finding.get("evidence", "")).strip()
            if not evidence:
                evidence = self._evidence_from_task(case["task_id"], finding)
            if not evidence or len(evidence) > 240 or "\n" in evidence or "\r" in evidence:
                continue
            if "evoagent:learned:%s:start" % rule_id in content:
                continue
            content = content.rstrip() + "\n\n" + self._learned_block(rule_id, finding, evidence)
            learned.append(rule_id)
            used_ids.append(case["id"])
        if not used_ids:
            return {
                "version": None, "decision": "deferred",
                "reason": "no supported SKILL.md mutation signal was found in unresolved feedback",
                "failure_cases_used": len(failures), "learned_rule_ids": [],
                "removed_rule_ids": [], "run_id": None,
            }
        candidate = validate_artifact({
            "name": skill_name,
            "files": {**artifact["files"], "SKILL.md": content},
        }, skill_name)
        result = self.propose(skill_name, candidate, tenant_id)
        result.update({
            "failure_cases_used": len(used_ids),
            "learned_rule_ids": sorted(set(learned)),
            "removed_rule_ids": sorted(set(removed)),
        })
        if result["decision"] == "activated":
            self.store.resolve_failure_cases(used_ids)
        return result

    # 按已验证规则和证据构造学习段落。
    @staticmethod
    def _learned_block(rule_id: str, finding: dict, evidence: str) -> str:
        evidence = evidence.replace("`", "'")
        severity = str(finding.get("severity", "medium")).lower()
        return "\n".join([
            "<!-- evoagent:learned:%s:start -->" % rule_id,
            "## Confirmed %s guidance" % rule_id, "",
            "Inspect added behavior equivalent to `%s`." % evidence,
            "Report `%s` at `%s` severity only when repository context confirms the defect."
            % (rule_id, severity),
            "Cite the changed line, explain impact, propose a minimal fix, and require a regression test.",
            "<!-- evoagent:learned:%s:end -->" % rule_id, "",
        ])

    # 删除指定规则的旧学习段落。
    @staticmethod
    def _remove_learned_block(content: str, rule_id: str) -> str:
        pattern = re.compile(
            r"\n*<!-- evoagent:learned:%s:start -->.*?"
            r"<!-- evoagent:learned:%s:end -->\n*" % (re.escape(rule_id), re.escape(rule_id)),
            re.DOTALL,
        )
        return pattern.sub("\n", content).rstrip() + "\n"

    # 从原始差异提取反馈位置证据。
    def _evidence_from_task(self, task_id: str, finding: dict) -> str:
        diff = self.store.get_task_payload(task_id)
        if not diff:
            return ""
        try:
            path, line = str(finding.get("path", "")), int(finding.get("line", 0))
            for changed in parse_unified_diff(diff).added_lines:
                if changed.path == path and changed.line == line:
                    return changed.content.strip()
        except (TypeError, ValueError):
            return ""
        return ""

    # 将指定历史版本重新设为生效版本。
    def rollback(self, skill_name: str, version: int, tenant_id: str = "default") -> bool:
        return self.store.activate_skill_artifact(skill_name, version, tenant_id)

    # 比较 Skill 正文和资源差异。
    @staticmethod
    def _skill_diff(baseline: dict, candidate: dict) -> dict:
        before, after = baseline.get("files") or {}, candidate.get("files") or {}
        names = sorted(set(before).union(after))
        return {
            "changed_files": [name for name in names if before.get(name) != after.get(name)],
            "baseline_skill_md_sha256": hashlib.sha256(
                str(before.get("SKILL.md", "")).encode("utf-8")
            ).hexdigest(),
            "candidate_skill_md_sha256": hashlib.sha256(
                str(after.get("SKILL.md", "")).encode("utf-8")
            ).hexdigest(),
        }

    # 创建具有完整字段的空评测结果。
    @staticmethod
    def _empty_metrics(cases: int) -> Dict[str, Any]:
        return {
            "schema_version": 2, "reviewer": "", "score": 0.0,
            "precision": 0.0, "recall": 0.0, "f1": 0.0,
            "severity_accuracy": 0.0, "high_severity_recall": 0.0,
            "clean_accuracy": 0.0, "cases": cases, "positive_cases": 0,
            "clean_cases": 0, "expected_findings": 0, "predicted_findings": 0,
            "successful_cases": 0, "success_rate": 0.0, "errors": [],
            "case_results": [],
        }

    # 提取公开版本元数据。
    @staticmethod
    def _public_version(value: dict) -> dict:
        return {
            key: value[key] for key in (
                "tenant_id", "skill_name", "version", "score", "active", "parent_version",
                "artifact_sha256", "created_at",
            ) if key in value
        }
