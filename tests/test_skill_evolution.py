# 作者：LHF
# 时间：2026-11-01
# 作用：验证标准 Skill 的发现、执行、演进及租户隔离。
import json
import os
import tempfile
import unittest

from evoagent.config import Settings
from evoagent.diff_parser import parse_unified_diff
from evoagent.service import ReviewService
from evoagent.skill_evolution import (
    AgentSkillReplayReviewer,
    SkillEvolutionEngine,
    validate_artifact,
)
from evoagent.skills import AgentSkill, SkillRegistry
from evoagent.store import TaskStore


RISK_DIFF = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+dangerous_call(data)\n"
CLEAN_DIFF = "--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-old\n+safe_call(data)\n"


# 构造标准 Skill Markdown 样例。
def skill_markdown(name="review-dangerous-calls", include_rule=True):
    guidance = (
        "\n## Confirmed SEC-DANGEROUS-CALL guidance\n\n"
        "Inspect added behavior equivalent to `dangerous_call(data)`.\n"
        "Report `SEC-DANGEROUS-CALL` at `high` severity when context confirms it.\n"
        if include_rule else ""
    )
    return """---
name: %s
description: Review added code for confirmed project-specific dangerous calls.
---

# Review dangerous calls

Use changed-line evidence and report only actionable defects.%s
""" % (name, guidance)


# 将正文及资源封装为 Skill 产物。
def artifact(name="review-dangerous-calls", include_rule=True):
    return validate_artifact(skill_markdown(name, include_rule), name)


class SkillAwareClient:
    provider = "fake"
    model = "skill-aware"

    # 返回角色的 JSON 决策并记录调用结果。
    def complete_json(self, role, _system, user, ledger=None, max_tokens=None):
        if ledger:
            ledger.record_model(role, self.provider, self.model, {
                "prompt_tokens": 10, "completion_tokens": 5,
            }, 1)
        managed = json.loads(user)
        task = json.loads(managed["task"])
        if role == "lead":
            if task["phase"] == "delegate":
                requested = task.get("requested_agent_skills") or []
                selected = requested or [
                    item["name"] for item in task.get("available_agent_skills") or []
                ]
                return {"action": "final", "delegations": [{
                    "assignment_id": "security-1", "worker": "security",
                    "objective": "Review project-specific dangerous calls",
                    "skills": selected,
                }], "risk_level": "normal"}
            if task["phase"] == "assess-workers":
                return {"action": "final", "revision_requests": [], "critic_objective": "Verify"}
            if task["phase"] == "finalize":
                return {
                    "action": "final",
                    "accepted_finding_indices": list(range(len(task["candidate_findings"]))),
                    "confidence_adjustments": [],
                }
        if role == "security":
            instructions = "\n".join(
                item.get("instructions", "") for item in task.get("active_agent_skills") or []
            )
            rendered_diff = json.dumps(task["diff"], ensure_ascii=False)
            if "dangerous_call(data)" in instructions and "dangerous_call(data)" in rendered_diff:
                return {"action": "final", "findings": [{
                    "rule_id": "SEC-DANGEROUS-CALL", "severity": "high",
                    "title": "Dangerous call", "explanation": "Confirmed unsafe API was added.",
                    "path": "a.py", "line": 1, "evidence": "dangerous_call(data)",
                    "fix": "Use safe_call instead.", "test": "Add a regression test.",
                    "confidence": .9,
                    "call_chain": [{"path": "a.py", "line": 1, "symbol": "dangerous_call"}],
                }]}
            return {"action": "final", "findings": []}
        if role == "correctness-reliability":
            return {"action": "final", "findings": []}
        if role == "critic":
            return {"action": "final", "decisions": [{
                "finding_index": index, "accepted": True, "objections": [],
                "confidence_adjustment": 0.0,
            } for index, _item in enumerate(task["candidates"])]}
        raise AssertionError(role)


class SkillEvolutionTests(unittest.TestCase):
    # 创建当前测试所需临时资源。
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.store = TaskStore(self.path)
        self.client = SkillAwareClient()

    # 清理当前测试的临时资源。
    def tearDown(self):
        os.unlink(self.path)

    # 保存训练、验证和保留集测试案例。
    def seed_cases(self):
        self.store.save_evaluation_case(
            "danger-validation", "validation", RISK_DIFF,
            [{"path": "a.py", "line": 1, "rule_id": "SEC-DANGEROUS-CALL", "min_severity": "high"}],
            "test",
        )
        self.store.save_evaluation_case("clean-holdout", "holdout", CLEAN_DIFF, [], "test")

    # 创建确定性回放演进引擎。
    def engine(self):
        return SkillEvolutionEngine(
            self.store,
            reviewer_factory=lambda value: AgentSkillReplayReviewer(value, self.client),
            min_cases=1, max_cases=10, min_improvement=.01, min_holdout_cases=1,
        )

    # 验证无需执行 Python 即可加载标准 Skill。
    def test_registry_loads_standard_skill_and_resources_without_python(self):
        with tempfile.TemporaryDirectory() as root:
            directory = os.path.join(root, "review-dangerous-calls")
            os.makedirs(os.path.join(directory, "references"))
            with open(os.path.join(directory, "SKILL.md"), "w", encoding="utf-8") as handle:
                handle.write(skill_markdown())
            with open(os.path.join(directory, "references", "policy.md"), "w", encoding="utf-8") as handle:
                handle.write("Use safe_call.\n")
            with open(os.path.join(directory, "skill.py"), "w", encoding="utf-8") as handle:
                handle.write("raise RuntimeError('must never be imported')\n")
            registry = SkillRegistry(root)
            registry.reload()
            loaded = registry.get_agent_skill("review-dangerous-calls")
            self.assertIsNotNone(loaded)
            self.assertEqual("Use safe_call.\n", loaded.read_resource("references/policy.md"))
            self.assertEqual("agent-skill", registry.list()[0]["kind"])
            os.unlink(os.path.join(directory, "SKILL.md"))
            registry.reload()
            self.assertIsNone(registry.get_agent_skill("review-dangerous-calls"))

    # 验证内置 Skill 可发现。
    def test_bundled_agent_skills_are_discoverable(self):
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "skills"))
        registry = SkillRegistry(root)
        registry.reload()
        self.assertEqual({
            "api-compatibility", "code-quality", "correctness-review",
            "database-review", "observability-review", "performance-review",
            "reliability-review", "security-review", "test-quality",
        }, {item["name"] for item in registry.catalog()})
        for item in registry.list():
            self.assertEqual("agent-skill", item["kind"])
            self.assertTrue(item["description"])

    # 验证元数据头及名称目录一致性。
    def test_skill_md_requires_frontmatter_and_matching_directory_name(self):
        with self.assertRaisesRegex(ValueError, "frontmatter"):
            AgentSkill.from_markdown("# Missing metadata")
        with self.assertRaisesRegex(ValueError, "match"):
            AgentSkill.from_markdown(
                skill_markdown("review-dangerous-calls"),
                expected_name="different-name",
            )

    # 验证选定正文进入工作角色流程。
    def test_agent_skill_body_is_selected_and_runs_through_worker_graph(self):
        reviewer = AgentSkillReplayReviewer(artifact(), self.client)
        findings = reviewer.review(RISK_DIFF, parse_unified_diff(RISK_DIFF))
        self.assertEqual(["SEC-DANGEROUS-CALL"], [item.rule_id for item in findings])
        summary = reviewer.agentic.collaboration_summary("skill-replay:review-dangerous-calls:1")
        self.assertEqual(["review-dangerous-calls"], summary["collaboration"]["agent_skills"])

    # 验证候选回放后激活并持久化。
    def test_candidate_skill_md_replay_activates_and_persists(self):
        self.seed_cases()
        result = self.engine().propose("review-dangerous-calls", artifact())
        self.assertEqual("activated", result["decision"])
        active = self.store.get_active_skill_artifact("review-dangerous-calls")
        self.assertEqual("agent-skill", active["artifact"]["format"])
        self.assertIn("SKILL.md", active["artifact"]["files"])
        self.assertEqual(2, active["artifact"]["schema_version"])
        self.assertEqual(["SKILL.md"], result["candidate_change"]["changed_files"])

    # 验证学习指导写入 Skill 正文。
    def test_auto_evolution_writes_learned_guidance_into_skill_md(self):
        self.seed_cases()
        self.store.create("task", "org/repo", 1, {"source": "test"})
        self.store.save_task_payload("task", RISK_DIFF)
        self.store.record_failure_case("task", "missed_issue", {"finding": {
            "rule_id": "SEC-DANGEROUS-CALL", "severity": "high", "path": "a.py", "line": 1,
        }})
        result = self.engine().auto_propose("review-dangerous-calls")
        self.assertEqual("activated", result["decision"])
        content = self.store.get_active_skill_artifact(
            "review-dangerous-calls"
        )["artifact"]["files"]["SKILL.md"]
        self.assertIn("Confirmed SEC-DANGEROUS-CALL guidance", content)
        self.assertIn("dangerous_call(data)", content)
        self.assertTrue(self.store.list_failure_cases()[0]["resolved"])

    # 验证租户版本隔离并覆盖同名磁盘 Skill。
    def test_active_agent_skills_are_tenant_isolated_and_override_disk(self):
        self.store.save_skill_artifact(
            "review-dangerous-calls", artifact(), 1.0, True, "tenant-a",
        )
        with tempfile.TemporaryDirectory() as skills_dir:
            settings = Settings(
                host="127.0.0.1", port=8080, db_path=self.path,
                max_diff_bytes=10000, max_steps=8, timeout_seconds=10,
                llm_base_url="", llm_api_key="", llm_model="",
                github_webhook_secret="", github_token="", auto_post_review=False,
                skills_dir=skills_dir, eval_min_holdout_cases=0,
            )
            service = ReviewService(settings)
            service.chat_client = self.client
            service.reviewer = service._build_agentic_reviewer()
            service.harness.reviewer = service.reviewer
            try:
                self.assertIn(
                    "review-dangerous-calls", {item["name"] for item in service.list_skills("tenant-a")}
                )
                self.assertNotIn(
                    "review-dangerous-calls", {item["name"] for item in service.list_skills("tenant-b")}
                )
                report = service.create_review(
                    "org/repo", RISK_DIFF, tenant_id="tenant-a",
                    enabled_skills=["review-dangerous-calls"],
                )["report"]
                self.assertIn(
                    "SEC-DANGEROUS-CALL", [item["rule_id"] for item in report["findings"]]
                )
            finally:
                service.queue.close()


if __name__ == "__main__":
    unittest.main()
