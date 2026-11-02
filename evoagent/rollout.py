# 作者：LHF
# 时间：2026-11-01
# 作用：管理确定性灰度分流、影子观察及自动回滚。
"""确定性灰度和影子分流，并支持自动回滚。"""
import hashlib
from typing import Dict, Optional


class ReleaseManager:
    # 保存灰度发布与影子观察使用的存储接口。
    def __init__(self, store):
        self.store = store

    # 校验并保存灰度、影子和错误预算。
    def configure(self, tenant_id: str, skill_name: str, config: Dict[str, object]) -> dict:
        canary = int(config.get("canary_percent", 0))
        shadow = int(config.get("shadow_percent", 0))
        if not 0 <= canary <= 100 or not 0 <= shadow <= 100:
            raise ValueError("canary_percent and shadow_percent must be between 0 and 100")
        if config.get("candidate_version") is None:
            raise ValueError("candidate_version is required")
        self.store.save_deployment(tenant_id, skill_name, config)
        return self.store.get_deployment(tenant_id, skill_name)

    # 确定租户、Skill 和任务的稳定分流。
    def assignment(self, tenant_id: str, skill_name: str, key: str) -> Dict[str, object]:
        deployment = self.store.get_deployment(tenant_id, skill_name)
        if not deployment or deployment["status"] != "running":
            return {"lane": "stable", "shadow": False, "deployment": None}
        bucket = int(hashlib.sha256(
            ("%s:%s:%s" % (tenant_id, skill_name, key)).encode("utf-8")
        ).hexdigest()[:8], 16) % 100
        return {
            "lane": "canary" if bucket < deployment["canary_percent"] else "stable",
            "shadow": bucket < deployment["shadow_percent"],
            "deployment": deployment,
        }

    # 记录发布结果并按错误预算回滚。
    def observe(
        self, tenant_id: str, skill_name: str, failed: bool,
        lane: str = "canary",
    ) -> Optional[dict]:
        if lane != "canary":
            return self.store.get_deployment(tenant_id, skill_name)
        result = self.store.record_deployment_result(tenant_id, skill_name, failed)
        if result and result["status"] == "rolled_back":
            self.store.create_alert(
                tenant_id, "rollout:%s" % skill_name, "critical",
                "Canary %s was automatically rolled back after exceeding its error budget." % skill_name,
            )
        return result

    # 记录稳定版和候选版的影子对比。
    def observe_shadow(
        self, tenant_id: str, skill_name: str, task_id: str, lane: str,
        primary: Dict[str, object], candidate: Optional[Dict[str, object]],
        candidate_failed: bool = False,
    ) -> Optional[dict]:
        primary_keys = set(primary.get("finding_keys", []))
        candidate_keys = set((candidate or {}).get("finding_keys", []))
        union = primary_keys | candidate_keys
        disagreement = len(primary_keys ^ candidate_keys) / len(union) if union else 0.0
        result = self.store.record_shadow_observation(
            tenant_id, skill_name, task_id, lane, primary, candidate,
            disagreement, candidate_failed,
        )
        if result and result["status"] == "promoted":
            self.store.create_alert(
                tenant_id, "rollout-promoted:%s" % skill_name, "info",
                "Candidate %s was automatically promoted after shadow verification." % skill_name,
            )
        return result
