# 作者：LHF
# 时间：2026-11-01
# 作用：执行带预算、重试、取消及检查点的工作流，并校验工具调用。
"""EvoAgent 无外部依赖的持久工作流运行时与工具注册表。

运行时将流程编排与角色行为分离：

* AgentRuntime 执行命名节点，管理预算、重试策略、取消检查及应用自有检查点。
使用工具的模型循环位于 agentic_core.BoundedRole 中。
持久化由应用存储层负责，因此工作进程重启不依赖框架专有的检查点格式。"""
from contextlib import nullcontext
from dataclasses import dataclass, field
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple


class RuntimeBudgetExceeded(RuntimeError):
    """配置的执行步数或实际耗时预算已耗尽。"""


class RuntimeCancelled(RuntimeError):
    """所属任务已请求取消执行。"""


class ToolProtocolError(RuntimeError):
    """工具请求不符合已注册的工具约定。"""


@dataclass(frozen=True)
class AgentTool:
    name: str
    description: str
    parameters: Dict[str, Any]
    handler: Callable[..., Any]

    # 返回能力发现阶段的目录信息。
    def catalog_entry(self) -> Dict[str, Any]:
        return {
            "name": self.name, "description": self.description,
            "parameters": self.parameters,
        }


class ToolRegistry:
    """显式工具目录，支持类似 JSON Schema 的参数校验。"""

    # 创建工具目录并逐个注册初始工具。
    def __init__(self, tools: Iterable[AgentTool] = ()):
        self._tools: Dict[str, AgentTool] = {}
        for tool in tools:
            self.register(tool)

    # 注册命名能力并检查重复约束。
    def register(self, tool: AgentTool) -> None:
        if not tool.name or tool.name in self._tools:
            raise ValueError("tool names must be non-empty and unique")
        self._tools[tool.name] = tool

    # 返回注册的工具名称。
    def names(self) -> List[str]:
        return sorted(self._tools)

    # 返回模型可见的能力目录。
    def catalog(self) -> List[Dict[str, Any]]:
        return [self._tools[name].catalog_entry() for name in self.names()]

    # 校验工具和参数后执行调用。
    def invoke(self, name: str, arguments: Dict[str, Any]) -> Any:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolProtocolError("unknown agent tool: %s" % name)
        self._validate(tool.parameters, arguments)
        return tool.handler(**arguments)

    # 检查必填字段、参数类型和取值。
    @staticmethod
    def _validate(schema: Dict[str, Any], arguments: Dict[str, Any]) -> None:
        if not isinstance(arguments, dict):
            raise ToolProtocolError("tool arguments must be an object")
        properties = dict(schema.get("properties") or {})
        required = set(schema.get("required") or [])
        missing = required.difference(arguments)
        if missing:
            raise ToolProtocolError(
                "missing required tool arguments: %s" % ", ".join(sorted(missing))
            )
        if schema.get("additionalProperties", False) is False:
            unknown = set(arguments).difference(properties)
            if unknown:
                raise ToolProtocolError(
                    "unknown tool arguments: %s" % ", ".join(sorted(unknown))
                )
        expected_types = {
            "string": str, "integer": int, "number": (int, float),
            "boolean": bool, "object": dict, "array": list,
        }
        for key, value in arguments.items():
            spec = properties.get(key) or {}
            expected = expected_types.get(spec.get("type"))
            if expected and (not isinstance(value, expected) or (
                spec.get("type") in {"integer", "number"} and isinstance(value, bool)
            )):
                raise ToolProtocolError(
                    "tool argument %s must be %s" % (key, spec.get("type"))
                )
            if isinstance(value, (int, float)):
                if "minimum" in spec and value < spec["minimum"]:
                    raise ToolProtocolError("tool argument %s is below minimum" % key)
                if "maximum" in spec and value > spec["maximum"]:
                    raise ToolProtocolError("tool argument %s exceeds maximum" % key)


@dataclass(frozen=True)
class RuntimeNode:
    name: str
    handler: Callable[[Dict[str, Any]], Dict[str, Any]]
    retries: Optional[int] = None
    checkpoint: bool = True


@dataclass(frozen=True)
class RuntimeEvent:
    kind: str
    node: str
    step: int
    attempt: int = 0
    detail: Dict[str, Any] = field(default_factory=dict)


class AgentRuntime:
    """无需第三方编排引擎，执行受预算约束的节点图。"""

    # 校验并保存工作流步数、耗时及节点重试限制。
    def __init__(
        self, max_steps: int = 8, timeout_seconds: int = 120,
        node_retries: int = 0,
    ):
        if max_steps < 1:
            raise ValueError("runtime max_steps must be at least 1")
        if timeout_seconds < 1:
            raise ValueError("runtime timeout_seconds must be at least 1")
        if node_retries < 0:
            raise ValueError("runtime node_retries cannot be negative")
        self.max_steps = max_steps
        self.timeout_seconds = timeout_seconds
        self.node_retries = node_retries

    # 执行节点并控制检查点、取消、预算和重试。
    def execute(
        self, initial_state: Dict[str, Any], nodes: Iterable[RuntimeNode],
        task_id: str = "", checkpoint_store=None,
        cancel_check: Optional[Callable[[], bool]] = None,
        event_sink: Optional[Callable[[RuntimeEvent], None]] = None,
        span_factory: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
        non_retryable: Tuple[type, ...] = (ValueError, RuntimeCancelled, RuntimeBudgetExceeded),
    ) -> Dict[str, Any]:
        state = dict(initial_state)
        started = time.monotonic()
        steps = 0
        checkpoints = (
            checkpoint_store.load_checkpoints(task_id)
            if checkpoint_store is not None and task_id else {}
        )

        # 发送当前节点执行事件。
        def emit(kind: str, node: str, attempt: int = 0, **detail) -> None:
            if event_sink:
                event_sink(RuntimeEvent(kind, node, steps, attempt, detail))

        # 检查取消、步数与耗时预算。
        def guard(node: str) -> None:
            if cancel_check and cancel_check():
                emit("cancelled", node)
                raise RuntimeCancelled("Task was cancelled")
            if steps >= self.max_steps or time.monotonic() - started > self.timeout_seconds:
                emit("budget_exhausted", node)
                raise RuntimeBudgetExceeded("task execution budget exceeded")

        for node in nodes:
            cached = checkpoints.get(node.name) if node.checkpoint else None
            # 已完成节点直接合并检查点输出，避免恢复时重复执行副作用。
            if cached and cached.get("status") == "completed":
                output = dict(cached.get("state") or {})
                state.update(output)
                emit("checkpoint_restored", node.name, int(cached.get("attempt", 0)))
                continue

            retries = self.node_retries if node.retries is None else node.retries
            previous_attempt = int((cached or {}).get("attempt", 0))
            last_error: Optional[Exception] = None
            # 尝试次数包括首次执行，恢复后沿用历史次数用于审计。
            for offset in range(1, retries + 2):
                guard(node.name)
                steps += 1
                attempt = previous_attempt + offset
                emit("node_started", node.name, attempt)
                try:
                    attrs = {
                        "task_id": task_id, "node": node.name,
                        "attempt": attempt, "runtime_step": steps,
                    }
                    context = (
                        span_factory("runtime.%s" % node.name, attrs)
                        if span_factory else nullcontext()
                    )
                    with context:
                        output = node.handler(state) or {}
                    if not isinstance(output, dict):
                        raise TypeError("runtime node %s must return a dict" % node.name)
                    state.update(output)
                    if checkpoint_store is not None and task_id and node.checkpoint:
                        checkpoint_store.save_checkpoint(
                            task_id, node.name, output, "completed", attempt
                        )
                    emit("node_completed", node.name, attempt, output_keys=sorted(output))
                    last_error = None
                    break
                # 取消、预算耗尽及确定性输入错误直接传播，不进入自动重试。
                except non_retryable:
                    raise
                except Exception as exc:
                    last_error = exc
                    if checkpoint_store is not None and task_id and node.checkpoint:
                        checkpoint_store.save_checkpoint(
                            task_id, node.name, {}, "failed", attempt, str(exc)
                        )
                    emit(
                        "node_failed", node.name, attempt,
                        error=str(exc)[:1000], will_retry=offset <= retries,
                    )
            if last_error is not None:
                raise last_error
        return state
