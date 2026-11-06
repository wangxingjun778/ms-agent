"""轻量工具调用的唯一结构体，不依赖 LLM 或接口框架。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional


class SubmissionNotSent(asyncio.CancelledError):
    """传输尚未开始，门闩或取消阻止了本次 HTTP 提交。"""


class ExternalTaskUnresolved(RuntimeError):
    """外部提交或任务结果未确认，停止自动规划，交由调用方对账。"""


@dataclass
class ToolResult:
    tool_name: str
    success: bool
    content: str
    raw_data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_error(
        cls, tool_name: str, error_msg: str, *, raw_data: Optional[Dict[str, Any]] = None, **metadata: Any
    ) -> 'ToolResult':
        return cls(tool_name, False, error_msg, raw_data=raw_data, error=error_msg, metadata=metadata)

    @classmethod
    def from_success(
        cls,
        tool_name: str,
        content: str,
        raw_data: Optional[Dict[str, Any]] = None,
        **metadata: Any,
    ) -> 'ToolResult':
        return cls(tool_name, True, content, raw_data=raw_data, metadata=metadata)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ToolResult':
        return cls(
            tool_name=str(data.get('tool_name', '')),
            success=bool(data.get('success', False)),
            content=str(data.get('content', '')),
            raw_data=data.get('raw_data'),
            error=data.get('error'),
            metadata=dict(data.get('metadata') or {}),
        )


@dataclass(frozen=True)
class ToolInvocationContext:
    user_id: str
    session_id: str
    run_uuid: str
    invocation_id: str
    trace_id: str = ''
    credentials: Dict[str, str] = field(default_factory=dict, repr=False)
    record: Optional[Callable[[str, Dict[str, Any]], Awaitable[None]]] = field(default=None, repr=False)
    guard: Optional[Callable[[], Awaitable[None]]] = field(default=None, repr=False)

    async def emit(self, event_type: str, data: Dict[str, Any]) -> None:
        if self.record is not None:
            await self.record(event_type, {'invocation_id': self.invocation_id, **data})
