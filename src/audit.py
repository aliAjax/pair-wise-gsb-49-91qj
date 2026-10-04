"""审计事件封装，时间线查询保持只读。"""
from typing import Any, Dict, List


class AuditRecorder:
    def __init__(self, repository: Any) -> None:
        self.repository = repository

    def timeline(self, entity_type: str, entity_id: int) -> List[Dict[str, Any]]:
        return self.repository.audit_timeline(entity_type, entity_id)

    def note(self, entity_type: str, entity_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        self.repository.add_audit(entity_type, entity_id, action, actor_id, details)
