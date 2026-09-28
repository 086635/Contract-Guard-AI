"""OpenAPI 成功包络构造器。

设计要点：
1. 所有 Service 路由共用同一个包络构造入口。
2. 响应字段使用生成物的 camelCase alias。
3. 不在路由中手拼 `dict` 包络。

对应：`文件集合/codex/4.C9配置与反馈服务.md`。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TypeVar

from app.generated.schemas import ApiEnvelopeBase, Code

_EnvelopeT = TypeVar("_EnvelopeT", bound=ApiEnvelopeBase)


def build_success_response(
    wrapper_cls: type[_EnvelopeT],
    data: object,
) -> _EnvelopeT:
    """构造统一成功响应。"""
    return wrapper_cls(
        code=Code.success,
        traceId=f"tr-{uuid.uuid4().hex[:12]}",
        serverTime=datetime.now(UTC),
        data=data,
    )


__all__ = ["build_success_response"]
