# 作者：LHF
# 时间：2026-11-01
# 作用：验证运行时、工具协议与记忆隔离及生命周期。
import os
import tempfile
import unittest
import json

from evoagent.agentic_core import BoundedRole
from evoagent.context_manager import ContextManager
from evoagent.memory import MemoryManager
from evoagent.runtime import (
    AgentRuntime, AgentTool, RuntimeNode, ToolProtocolError, ToolRegistry,
)
from evoagent.store import TaskStore, utc_now
from evoagent.telemetry import ExecutionLedger


class RuntimeMemoryTests(unittest.TestCase):
    # 创建当前测试所需临时资源。
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.store = TaskStore(self.path)

    # 清理当前测试的临时资源。
    def tearDown(self):
        os.unlink(self.path)

    # 验证运行时恢复已完成节点。
    def test_runtime_restores_completed_node_checkpoints(self):
        self.store.create("runtime-task", "org/repo", 1, {})
        runtime = AgentRuntime(max_steps=4, timeout_seconds=5)
        calls = []
        nodes = [
            RuntimeNode("plan", lambda _state: calls.append("plan") or {"value": 2}),
            RuntimeNode(
                "execute",
                lambda state: calls.append("execute") or {"result": state["value"] * 3},
            ),
        ]

        first = runtime.execute({}, nodes, "runtime-task", self.store)
        second = runtime.execute({}, nodes, "runtime-task", self.store)

        self.assertEqual(6, first["result"])
        self.assertEqual(6, second["result"])
        self.assertEqual(["plan", "execute"], calls)

    # 验证调用工具前校验参数。
    def test_tool_registry_validates_arguments_before_invocation(self):
        calls = []
        registry = ToolRegistry([AgentTool(
            "lookup", "Lookup one key.",
            {
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"], "additionalProperties": False,
            },
            lambda key: calls.append(key) or "value:%s" % key,
        )])

        with self.assertRaisesRegex(ToolProtocolError, "unknown tool arguments"):
            registry.invoke("lookup", {"key": 7, "unexpected": True})

        self.assertEqual([], calls)
        self.assertEqual("value:x", registry.invoke("lookup", {"key": "x"}))
        self.assertEqual(["x"], calls)

    # 验证记忆按仓库及租户隔离。
    def test_memory_recall_is_repository_and_tenant_isolated(self):
        memory = MemoryManager(self.store, recall_limit=5)
        memory.remember(
            "tenant-a", "org/repo", "semantic", "review_feedback",
            "SEC-EVAL was a confirmed missed issue in authentication code",
            importance=0.9,
        )
        memory.remember(
            "tenant-b", "org/repo", "semantic", "review_feedback",
            "REL-DEBUG-PRINT was accepted", importance=0.9,
        )

        recalled = memory.recall(
            "tenant-a", "org/repo", "authentication SEC-EVAL security"
        )

        self.assertEqual(1, len(recalled))
        self.assertIn("SEC-EVAL", recalled[0]["content"])
        self.assertEqual([], memory.recall("tenant-a", "org/other", "SEC-EVAL"))

    # 验证召回前清理过期记录。
    def test_memory_recall_purges_expired_records(self):
        self.store.save_agent_memory({
            "id": "expired-memory", "tenant_id": "tenant-a",
            "repository": "org/repo", "task_id": "old-task", "agent": "agent",
            "scope": "working", "kind": "observation", "content": "expired content",
            "keywords": ["expired"], "metadata": {}, "importance": 0.5,
            "created_at": utc_now(), "expires_at": "2000-01-01T00:00:00+00:00",
        })

        MemoryManager(self.store).recall("tenant-a", "org/repo", "expired")

        self.assertEqual([], self.store.list_agent_memories(
            "tenant-a", "org/repo", ("working",), 10
        ))

    # 验证观察保存后对后续步骤可见。
    def test_tool_observation_is_written_and_visible_to_next_agent_step(self):
        memory = MemoryManager(self.store, recall_limit=8)
        seen = []

        class ToolThenFinalClient:
            provider = "fake"
            model = "fake"

            # 返回角色的 JSON 决策并记录调用结果。
            def complete_json(_self, _role, _prompt, user, ledger=None, max_tokens=None):
                managed = json.loads(user)
                task = json.loads(managed["task"])
                seen.append(task.get("working_memory"))
                if ledger:
                    ledger.record_model("security", "fake", "fake", {
                        "prompt_tokens": 10, "completion_tokens": 3,
                    }, 1)
                if len(seen) == 1:
                    return {"action": "tool", "tool": "lookup", "arguments": {"key": "auth"}}
                return {"action": "final", "findings": []}

        # 读取当前角色在本任务中可见的工作记忆。
        def supplier():
            values = memory.recall_working("tenant-a", "org/repo", "task")
            return ContextManager.format_memories(values) if values else None

        # 将角色产生的工具观察写入工作记忆。
        def sink(agent, observation):
            memory.remember_observation(
                "tenant-a", "org/repo", "task", agent, observation,
            )

        role = BoundedRole(
            "security", "Use facts.", ToolThenFinalClient(), 1000, 10,
            context_manager=ContextManager(), working_memory_supplier=supplier,
            observation_sink=sink,
        )
        registry = ToolRegistry([AgentTool(
            "lookup", "Lookup a fact.", {
                "type": "object", "properties": {"key": {"type": "string"}},
                "required": ["key"], "additionalProperties": False,
            }, lambda key: {"evidence_id": "lookup:%s" % key, "output": {"value": "allowed"}},
        )])

        role.run(json.dumps({"phase": "worker"}), registry, ExecutionLedger("agentic"))

        self.assertIsNone(seen[0])
        self.assertEqual("tool_observation", seen[1]["items"][0]["kind"])
        self.assertIn("lookup:auth", seen[1]["items"][0]["content"])

    # 验证归档清理工作记忆并保留情景。
    def test_task_consolidation_releases_working_memory_but_keeps_episode(self):
        memory = MemoryManager(self.store)
        memory.remember_observation("tenant-a", "org/repo", "task", "security", {
            "step": 1, "tool": "symbol", "ok": True,
            "result": {"evidence_id": "symbol:auth", "output": {"callers": ["api"]}},
        })

        memory.consolidate_task("tenant-a", "org/repo", "task", {"accepted_findings": []})

        self.assertEqual([], memory.recall_working("tenant-a", "org/repo", "task"))
        archived = memory.recall("tenant-a", "org/repo", "completed task")
        self.assertEqual("task_summary", archived[0]["kind"])

    # 验证工作记忆按角色隔离。
    def test_working_memory_is_isolated_by_agent_role(self):
        memory = MemoryManager(self.store)
        memory.remember_observation("tenant-a", "org/repo", "task", "security", {
            "step": 1, "tool": "lookup", "ok": True,
            "result": {"evidence_id": "security:auth", "output": "security fact"},
        })
        memory.remember_observation("tenant-a", "org/repo", "task", "correctness-reliability", {
            "step": 1, "tool": "lookup", "ok": True,
            "result": {"evidence_id": "correctness:auth", "output": "correctness fact"},
        })

        security = memory.recall_working(
            "tenant-a", "org/repo", "task", agent="security",
        )
        critic = memory.recall_working(
            "tenant-a", "org/repo", "task", agent="critic",
        )

        self.assertEqual(1, len(security))
        self.assertIn("security:auth", security[0]["content"])
        self.assertEqual([], critic)


if __name__ == "__main__":
    unittest.main()
