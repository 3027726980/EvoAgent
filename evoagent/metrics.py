# 作者：LHF
# 时间：2026-11-01
# 作用：收集计数与耗时并输出 Prometheus 指标。
import threading
import time
from collections import defaultdict
from contextlib import contextmanager


class Metrics:
    # 创建线程锁及计数、耗时累计容器。
    def __init__(self):
        self._lock = threading.Lock()
        self.counters = defaultdict(float)
        self.duration_sum = defaultdict(float)
        self.duration_count = defaultdict(int)

    # 累加命名指标。
    def inc(self, name: str, value: float = 1.0) -> None:
        with self._lock:
            self.counters[name] += value

    # 记录执行耗时与调用次数。
    @contextmanager
    def timer(self, name: str):
        started = time.monotonic()
        try:
            yield
        finally:
            elapsed = time.monotonic() - started
            with self._lock:
                self.duration_sum[name] += elapsed
                self.duration_count[name] += 1

    # 输出 Prometheus 格式指标。
    def prometheus(self) -> str:
        with self._lock:
            lines = []
            for name, value in sorted(self.counters.items()):
                lines.extend(["# TYPE evoagent_%s counter" % name, "evoagent_%s %s" % (name, value)])
            for name, value in sorted(self.duration_sum.items()):
                lines.extend([
                    "# TYPE evoagent_%s_seconds summary" % name,
                    "evoagent_%s_seconds_sum %s" % (name, value),
                    "evoagent_%s_seconds_count %s" % (name, self.duration_count[name]),
                ])
        return "\n".join(lines) + "\n"


metrics = Metrics()

