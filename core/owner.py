"""主人判定：与平台无关，只比字符串。

- `aiocqhttp`（NapCat）：sender_id 是数字 QQ
- `qq_official`（官方机器人）：sender_id 是 openid 那串
两种情况都用同一个配置项，不做平台特判。
"""
from __future__ import annotations


class OwnerGuard:
    def __init__(self, owner_id: str = "") -> None:
        self.owner_id = str(owner_id or "").strip()
        self.observed = ""

    def is_owner(self, sender_id: str, *, is_private: bool) -> bool:
        if not is_private:
            return False
        sender = str(sender_id or "").strip()
        if not sender:
            return False
        if not self.owner_id:
            if not self.observed:
                self.observed = sender
            return sender == self.observed
        return sender == self.owner_id

    @property
    def bound(self) -> str:
        return self.owner_id or self.observed
