"""I/O schema for the lightweight agent system."""
from __future__ import annotations

import dataclasses
import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

__all__ = [
    'AgentRequest',
    'AgentResponse',
    'Attachment',
    'EventType',
    'MessageInput',
    'MessageOutput',
    'StreamEvent',
    'ToolCallInfo',
    'ToolCallRecord',
    'ToolResult',
    'UsageStats',
]


@dataclass
class Attachment:
    """An attachment supplied with an input message."""

    type: str
    url: Optional[str] = None
    base64: Optional[str] = None
    filename: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Attachment':
        """Create an attachment while ignoring unknown transport fields."""
        return cls(**{
            key: value
            for key, value in data.items() if key in cls.__dataclass_fields__
        })


@dataclass
class MessageInput:
    """A message accepted by the lightweight agent API."""

    role: str
    content: str
    attachments: Optional[List[Attachment]] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'MessageInput':
        """Deserialize an input message and its attachments."""
        attachments = data.get('attachments')
        parsed = None
        if isinstance(attachments, list):
            parsed = [
                item if isinstance(item, Attachment) else Attachment.from_dict(item)
                for item in attachments if isinstance(item, (Attachment, dict))
            ]
        return cls(
            role=str(data.get('role', 'user')),
            content=str(data.get('content', '')),
            attachments=parsed,
        )


@dataclass
class AgentRequest:
    """A complete request to execute one agent turn."""

    user_id: str
    session_id: str
    messages: List[MessageInput]
    config_overrides: Optional[Dict[str, Any]] = None
    stream: bool = True

    @classmethod
    def new_session_id(cls) -> str:
        """Return a globally unique session identifier."""
        return str(uuid.uuid4())

    def to_dict(self) -> Dict[str, Any]:
        """Serialize this request into JSON-compatible primitives."""
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'AgentRequest':
        """Deserialize a request from a mapping."""
        raw_messages = data.get('messages') or []
        messages = [
            item if isinstance(item, MessageInput) else MessageInput.from_dict(item)
            for item in raw_messages if isinstance(item, (MessageInput, dict))
        ]
        session_id = data.get('session_id') or cls.new_session_id()
        overrides = data.get('config_overrides')
        return cls(
            user_id=str(data.get('user_id', 'default')),
            session_id=str(session_id),
            messages=messages,
            config_overrides=dict(overrides) if isinstance(overrides, dict) else None,
            stream=bool(data.get('stream', True)),
        )


@dataclass
class ToolResult:
    """Normalized result returned by a tool invocation."""

    tool_name: str
    success: bool
    content: str
    raw_data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_error(cls, tool_name: str, error_msg: str) -> 'ToolResult':
        """Create a failed tool result."""
        return cls(
            tool_name=tool_name,
            success=False,
            content=error_msg,
            error=error_msg,
        )

    @classmethod
    def from_success(
        cls,
        tool_name: str,
        content: str,
        raw_data: Optional[Dict[str, Any]] = None,
    ) -> 'ToolResult':
        """Create a successful tool result."""
        return cls(
            tool_name=tool_name,
            success=True,
            content=content,
            raw_data=raw_data,
        )

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ToolResult':
        """Deserialize a tool result."""
        raw_data = data.get('raw_data')
        metadata = data.get('metadata')
        return cls(
            tool_name=str(data.get('tool_name', '')),
            success=bool(data.get('success', False)),
            content=str(data.get('content', '')),
            raw_data=dict(raw_data) if isinstance(raw_data, dict) else None,
            error=None if data.get('error') is None else str(data['error']),
            metadata=dict(metadata) if isinstance(metadata, dict) else {},
        )


@dataclass
class ToolCallRecord:
    """A completed tool call and its timing information."""

    tool_name: str
    arguments: Dict[str, Any]
    result: ToolResult
    duration_ms: int

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ToolCallRecord':
        """Deserialize a tool call record."""
        result = data.get('result') or {}
        if not isinstance(result, ToolResult):
            result = ToolResult.from_dict(result if isinstance(result, dict) else {})
        arguments = data.get('arguments')
        return cls(
            tool_name=str(data.get('tool_name', result.tool_name)),
            arguments=dict(arguments) if isinstance(arguments, dict) else {},
            result=result,
            duration_ms=int(data.get('duration_ms', 0) or 0),
        )


@dataclass
class ToolCallInfo:
    """Tool call requested by an assistant message."""

    id: str
    name: str
    arguments: Dict[str, Any]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ToolCallInfo':
        """Deserialize tool call information."""
        arguments = data.get('arguments')
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (TypeError, ValueError):
                arguments = {}
        return cls(
            id=str(data.get('id', '')),
            name=str(data.get('name') or data.get('tool_name') or ''),
            arguments=dict(arguments) if isinstance(arguments, dict) else {},
        )


@dataclass
class MessageOutput:
    """A message produced during agent execution."""

    role: str
    content: str
    tool_calls: Optional[List[ToolCallInfo]] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'MessageOutput':
        """Deserialize an output message and its tool calls."""
        raw_calls = data.get('tool_calls')
        calls = None
        if isinstance(raw_calls, list):
            calls = [
                item if isinstance(item, ToolCallInfo) else ToolCallInfo.from_dict(item)
                for item in raw_calls if isinstance(item, (ToolCallInfo, dict))
            ]
        return cls(
            role=str(data.get('role', 'assistant')),
            content=str(data.get('content', '')),
            tool_calls=calls,
        )


@dataclass
class UsageStats:
    """Aggregate token and loop usage for an agent request."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    rounds: int = 0

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'UsageStats':
        """Deserialize usage counters, deriving total tokens when omitted."""
        prompt = int(data.get('prompt_tokens', 0) or 0)
        completion = int(data.get('completion_tokens', 0) or 0)
        total = int(data.get('total_tokens', prompt + completion) or 0)
        return cls(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
            rounds=int(data.get('rounds', 0) or 0),
        )


@dataclass
class AgentResponse:
    """Collected result of a completed agent request."""

    session_id: str
    messages: List[MessageOutput] = field(default_factory=list)
    tool_calls_made: List[ToolCallRecord] = field(default_factory=list)
    usage: UsageStats = field(default_factory=UsageStats)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize this response into JSON-compatible primitives."""
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'AgentResponse':
        """Deserialize a response from a mapping."""
        raw_messages = data.get('messages') or []
        raw_records = data.get('tool_calls_made') or []
        usage = data.get('usage') or {}
        metadata = data.get('metadata')
        return cls(
            session_id=str(data.get('session_id', '')),
            messages=[
                item if isinstance(item, MessageOutput) else MessageOutput.from_dict(item)
                for item in raw_messages if isinstance(item, (MessageOutput, dict))
            ],
            tool_calls_made=[
                item if isinstance(item, ToolCallRecord) else ToolCallRecord.from_dict(item)
                for item in raw_records if isinstance(item, (ToolCallRecord, dict))
            ],
            usage=usage if isinstance(usage, UsageStats) else UsageStats.from_dict(usage),
            metadata=dict(metadata) if isinstance(metadata, dict) else {},
        )

    @classmethod
    def from_events(
        cls,
        session_id: str,
        events: List['StreamEvent'],
    ) -> 'AgentResponse':
        """Build a response from streaming events.

        The collector accepts both ``StreamEvent`` instances and their serialized
        dictionary form. It tolerates partial streams by materializing accumulated
        assistant deltas when no complete message was emitted.
        """
        messages: List[MessageOutput] = []
        tool_records: List[ToolCallRecord] = []
        metadata: Dict[str, Any] = {}
        usage = UsageStats()
        delta_parts: List[str] = []
        pending_calls: List[ToolCallInfo] = []
        completed_rounds = 0

        for raw_event in events:
            event = raw_event if isinstance(raw_event, StreamEvent) else StreamEvent.from_dict(raw_event)
            event_type = event.event_type
            data = event.data

            if event_type == EventType.LLM_DELTA:
                delta = data.get('delta', data.get('content', ''))
                if delta is not None:
                    delta_parts.append(str(delta))
            elif event_type == EventType.LLM_TOOL_CALL:
                payload = data.get('tool_call', data)
                if isinstance(payload, dict):
                    pending_calls.append(ToolCallInfo.from_dict(payload))
            elif event_type == EventType.TOOL_RESULT:
                payload = data.get('record', data)
                if isinstance(payload, dict) and 'tool_name' in payload:
                    if 'result' not in payload:
                        payload = {
                            **payload,
                            'result': {
                                'tool_name': payload.get('tool_name', ''),
                                'success': payload.get('success', False),
                                'content': payload.get('content', ''),
                                'raw_data': payload.get('raw_data'),
                                'error': payload.get('error'),
                                'metadata': payload.get('metadata', {}),
                            },
                        }
                    tool_records.append(ToolCallRecord.from_dict(payload))
            elif event_type == EventType.LLM_COMPLETE:
                completed_rounds += 1
                raw_messages = data.get('messages')
                raw_message = data.get('message')
                if isinstance(raw_messages, list):
                    messages.extend(
                        item if isinstance(item, MessageOutput) else MessageOutput.from_dict(item)
                        for item in raw_messages if isinstance(item, (MessageOutput, dict)))
                elif isinstance(raw_message, (MessageOutput, dict)):
                    messages.append(
                        raw_message if isinstance(raw_message, MessageOutput) else MessageOutput.from_dict(raw_message))
                elif 'role' in data or 'content' in data:
                    messages.append(MessageOutput.from_dict(data))
                elif delta_parts or pending_calls:
                    messages.append(
                        MessageOutput(
                            role='assistant',
                            content=''.join(delta_parts),
                            tool_calls=list(pending_calls) or None,
                        ))
                delta_parts.clear()
                pending_calls.clear()
                if isinstance(data.get('usage'), dict):
                    usage = UsageStats.from_dict(data['usage'])
            elif event_type == EventType.STREAM_END:
                response = data.get('response')
                if isinstance(response, dict):
                    completed = cls.from_dict(response)
                    if completed.messages:
                        messages = completed.messages
                    if completed.tool_calls_made:
                        tool_records = completed.tool_calls_made
                    usage = completed.usage
                    metadata.update(completed.metadata)
                if isinstance(data.get('usage'), dict):
                    usage = UsageStats.from_dict(data['usage'])
                if isinstance(data.get('metadata'), dict):
                    metadata.update(data['metadata'])
            elif event_type == EventType.ERROR:
                errors = metadata.setdefault('errors', [])
                errors.append(dict(data))

        if delta_parts or pending_calls:
            messages.append(
                MessageOutput(
                    role='assistant',
                    content=''.join(delta_parts),
                    tool_calls=pending_calls or None,
                ))
        if usage.rounds == 0:
            usage.rounds = completed_rounds
        if usage.total_tokens == 0:
            usage.total_tokens = usage.prompt_tokens + usage.completion_tokens
        return cls(
            session_id=session_id,
            messages=messages,
            tool_calls_made=tool_records,
            usage=usage,
            metadata=metadata,
        )


class EventType(str, Enum):
    """Event names emitted by the lightweight streaming API."""

    STREAM_START = 'stream_start'
    LLM_DELTA = 'llm_delta'
    LLM_TOOL_CALL = 'llm_tool_call'
    TOOL_START = 'tool_start'
    TOOL_RESULT = 'tool_result'
    LLM_COMPLETE = 'llm_complete'
    STREAM_END = 'stream_end'
    ERROR = 'error'


@dataclass
class StreamEvent:
    """One ordered event in an agent response stream."""

    event_type: str
    data: Dict[str, Any]
    seq: int
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize this event into JSON-compatible primitives."""
        result = dataclasses.asdict(self)
        if isinstance(self.event_type, Enum):
            result['event_type'] = self.event_type.value
        return result

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'StreamEvent':
        """Deserialize a stream event from a mapping."""
        event_type = data.get('event_type', data.get('type', ''))
        if isinstance(event_type, Enum):
            event_type = event_type.value
        payload = data.get('data')
        return cls(
            event_type=str(event_type),
            data=dict(payload) if isinstance(payload, dict) else {},
            seq=int(data.get('seq', 0) or 0),
            timestamp=float(data.get('timestamp', time.time()) or time.time()),
        )
