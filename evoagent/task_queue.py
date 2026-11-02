# 作者：LHF
# 时间：2026-11-01
# 作用：管理内存或 Redis 任务投递、租约恢复、重试与死信。
"""支持确认、租约、退避重试与死信队列的持久任务投递。"""
import json
import queue
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Optional


class PermanentTaskError(RuntimeError):
    """不应重试的永久性任务错误。"""


class TaskQueue:
    STREAM = "evoagent:review:stream"
    DLQ = "evoagent:review:dlq"
    GROUP = "evoagent-workers"

    # 配置处理回调与重试策略，并选择内存或 Redis 投递后端。
    def __init__(
        self, handler: Callable[[Dict[str, Any]], None], workers: int = 2,
        redis_url: str = "", max_attempts: int = 3, lease_seconds: int = 60,
        on_dead_letter: Optional[Callable[[Dict[str, Any], str], None]] = None,
    ):
        self.handler = handler
        self.redis_url = redis_url
        self.max_attempts = max_attempts
        self.lease_seconds = lease_seconds
        self.on_dead_letter = on_dead_letter
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="evoagent-worker")
        self._redis = None
        self._memory: queue.Queue = queue.Queue()
        self._memory_dlq = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.consumer = "%s-%s" % (socket.gethostname(), uuid.uuid4().hex[:8])
        if redis_url:
            try:
                import redis
            except ImportError as exc:
                raise RuntimeError("Redis mode requires: pip install redis") from exc
            self._redis = redis.Redis.from_url(redis_url, decode_responses=True)
            self._redis.ping()
            try:
                self._redis.xgroup_create(self.STREAM, self.GROUP, id="0", mkstream=True)
            except redis.ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise
            for _ in range(workers):
                self._executor.submit(self._redis_worker)

    # 返回当前队列后端名称。
    @property
    def backend(self) -> str:
        return "redis-streams" if self._redis else "memory-acked"

    # 封装任务信息并投递到队列。
    def submit(self, payload: Dict[str, Any], message_id: str = "") -> str:
        envelope = {
            "message_id": message_id or str(payload.get("task_id") or uuid.uuid4()),
            "attempt": 0,
            "payload": payload,
            "submitted_at": time.time(),
        }
        if self._redis:
            self._redis.xadd(self.STREAM, {"envelope": json.dumps(envelope, ensure_ascii=False)})
        else:
            self._executor.submit(self._deliver, envelope)
        return envelope["message_id"]

    # 执行任务并决定重试或进入死信。
    def _deliver(self, envelope: Dict[str, Any]) -> bool:
        envelope["attempt"] = int(envelope.get("attempt", 0)) + 1
        try:
            self.handler(envelope["payload"])
            return True
        except PermanentTaskError as exc:
            self._dead_letter(envelope, str(exc))
            return False
        except Exception as exc:
            if envelope["attempt"] >= self.max_attempts:
                self._dead_letter(envelope, str(exc))
            elif self._redis:
                self._redis.xadd(self.STREAM, {
                    "envelope": json.dumps(envelope, ensure_ascii=False)
                })
            else:
                delay = min(2 ** (envelope["attempt"] - 1), 10)
                timer = threading.Timer(delay, self._submit_memory, args=(envelope,))
                timer.daemon = True
                timer.start()
            return False

    # 向进程内线程池提交任务。
    def _submit_memory(self, envelope: Dict[str, Any]) -> None:
        if not self._stop.is_set():
            self._executor.submit(self._deliver, envelope)

    # 消费消息并在安全处理后确认。
    def _redis_worker(self) -> None:
        while not self._stop.is_set():
            self._reclaim_stale()
            messages = self._redis.xreadgroup(
                self.GROUP, self.consumer, {self.STREAM: ">"}, count=1, block=1000
            )
            for _stream, entries in messages:
                for redis_id, fields in entries:
                    try:
                        envelope = json.loads(fields["envelope"])
                    except Exception as exc:
                        envelope = {
                            "message_id": redis_id, "attempt": self.max_attempts,
                            "payload": {}, "submitted_at": time.time(),
                        }
                        self._dead_letter(envelope, "invalid queue envelope: %s" % exc)
                        self._redis.xack(self.STREAM, self.GROUP, redis_id)
                        continue
                    try:
                        self._deliver(envelope)
                        # 任务完成，或已安全重新入队、进入死信队列后，才确认原消息。
                        self._redis.xack(self.STREAM, self.GROUP, redis_id)
                    except Exception:
                        # 基础设施故障时保留待确认状态，交由租约恢复机制接管。
                        continue

    # 回收租约过期的未确认消息。
    def _reclaim_stale(self) -> None:
        try:
            result = self._redis.xautoclaim(
                self.STREAM, self.GROUP, self.consumer,
                min_idle_time=self.lease_seconds * 1000, start_id="0-0", count=10,
            )
            entries = result[1] if len(result) > 1 else []
            for redis_id, fields in entries:
                envelope = json.loads(fields["envelope"])
                self._deliver(envelope)
                self._redis.xack(self.STREAM, self.GROUP, redis_id)
        except Exception:
            # 不支持 XAUTOCLAIM 的 Redis 版本仍可处理新消息。
            return

    # 保存最终失败消息并通知回调。
    def _dead_letter(self, envelope: Dict[str, Any], error: str) -> None:
        item = {**envelope, "error": error[:2000], "failed_at": time.time()}
        if self._redis:
            self._redis.xadd(self.DLQ, {"envelope": json.dumps(item, ensure_ascii=False)})
        else:
            with self._lock:
                self._memory_dlq.append(item)
        if self.on_dead_letter:
            self.on_dead_letter(envelope.get("payload") or {}, item["error"])

    # 读取数量受限的死信消息。
    def dead_letters(self, limit: int = 100) -> list:
        if self._redis:
            rows = self._redis.xrevrange(self.DLQ, count=max(1, min(limit, 500)))
            return [json.loads(fields["envelope"]) for _id, fields in rows]
        with self._lock:
            return list(reversed(self._memory_dlq[-limit:]))

    # 重置尝试次数并重新投递死信。
    def replay_dead_letter(self, message_id: str) -> bool:
        for item in self.dead_letters(500):
            if item.get("message_id") == message_id:
                payload = item.get("payload") or {}
                self.submit(payload, message_id=message_id)
                return True
        return False

    # 停止队列并关闭执行器。
    def close(self) -> None:
        self._stop.set()
        self._executor.shutdown(wait=False)
