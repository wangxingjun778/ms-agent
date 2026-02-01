# Copyright (c) ModelScope Contributors. All rights reserved.
"""
Event bus module for decoupled component communication.

The event bus enables loose coupling between components by allowing
them to publish and subscribe to events without direct dependencies.
"""
import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import (TYPE_CHECKING, Any, Callable, Coroutine, Dict, List,
                    Optional)

from ms_agent.utils.logger import get_logger

if TYPE_CHECKING:
    from .context import AgentContext

logger = get_logger()

# Type alias for event handlers
EventHandler = Callable[['AgentEvent'], Coroutine[Any, Any, None]]


class EventType(Enum):
    """Standard event types for agent lifecycle."""

    # Task lifecycle events
    TASK_BEGIN = auto()
    TASK_END = auto()

    # Round lifecycle events
    ROUND_BEGIN = auto()
    ROUND_END = auto()

    # LLM events
    LLM_GENERATE_START = auto()
    LLM_GENERATE_END = auto()
    LLM_STREAM_CHUNK = auto()

    # Tool events
    TOOL_CALL_START = auto()
    TOOL_CALL_END = auto()

    # Memory events
    MEMORY_RETRIEVE = auto()
    MEMORY_STORE = auto()

    # Skill events
    SKILL_ANALYZE_START = auto()
    SKILL_ANALYZE_END = auto()
    SKILL_EXECUTE_START = auto()
    SKILL_EXECUTE_END = auto()

    # Error events
    ERROR = auto()
    WARNING = auto()

    # Custom events
    CUSTOM = auto()


@dataclass
class AgentEvent:
    """
    Event data container.

    Attributes:
        type: Event type identifier.
        data: Event payload data.
        source: Event source identifier.
        timestamp: Event creation time.
        context: Optional agent context reference.
    """
    type: EventType
    data: Any = None
    source: str = ''
    timestamp: datetime = field(default_factory=datetime.now)
    context: Optional['AgentContext'] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize event to dictionary."""
        return {
            'type': self.type.name,
            'data': self.data,
            'source': self.source,
            'timestamp': self.timestamp.isoformat(),
        }


class AgentEventBus:
    """
    Event bus for agent communication.

    Provides publish-subscribe pattern for decoupled component communication.
    Supports both synchronous and asynchronous event handling.

    Features:
    - Multiple handlers per event type
    - Wildcard subscriptions (receive all events)
    - Handler priority ordering
    - Async handler execution
    - Event history tracking

    Attributes:
        _handlers: Event type to handlers mapping.
        _wildcard_handlers: Handlers receiving all events.
        _history: Event history for debugging.
        _history_enabled: Whether to track history.
        _max_history: Maximum history entries to keep.
    """

    def __init__(self, history_enabled: bool = False, max_history: int = 100):
        """
        Initialize event bus.

        Args:
            history_enabled: Enable event history tracking.
            max_history: Maximum history entries.
        """
        self._handlers: Dict[EventType, List[tuple]] = defaultdict(list)
        self._wildcard_handlers: List[tuple] = []
        self._history: List[AgentEvent] = []
        self._history_enabled = history_enabled
        self._max_history = max_history

    def subscribe(self,
                  event_type: EventType,
                  handler: EventHandler,
                  priority: int = 0) -> None:
        """
        Subscribe a handler to an event type.

        Args:
            event_type: Event type to subscribe to.
            handler: Async handler function.
            priority: Handler priority (higher = earlier execution).
        """
        self._handlers[event_type].append((priority, handler))
        self._handlers[event_type].sort(key=lambda x: -x[0])
        logger.debug(
            f'Subscribed handler to {event_type.name} with priority {priority}'
        )

    def subscribe_all(self, handler: EventHandler, priority: int = 0) -> None:
        """
        Subscribe a handler to all events.

        Args:
            handler: Async handler function.
            priority: Handler priority.
        """
        self._wildcard_handlers.append((priority, handler))
        self._wildcard_handlers.sort(key=lambda x: -x[0])
        logger.debug(f'Subscribed wildcard handler with priority {priority}')

    def unsubscribe(self, event_type: EventType,
                    handler: EventHandler) -> bool:
        """
        Unsubscribe a handler from an event type.

        Args:
            event_type: Event type.
            handler: Handler to remove.

        Returns:
            True if handler was found and removed.
        """
        handlers = self._handlers.get(event_type, [])
        for i, (_, h) in enumerate(handlers):
            if h == handler:
                handlers.pop(i)
                return True
        return False

    def unsubscribe_all_for_type(self, event_type: EventType) -> int:
        """
        Remove all handlers for an event type.

        Args:
            event_type: Event type to clear.

        Returns:
            Number of handlers removed.
        """
        count = len(self._handlers.get(event_type, []))
        self._handlers[event_type] = []
        return count

    async def publish(self, event: AgentEvent) -> None:
        """
        Publish an event to all subscribed handlers.

        Handlers are executed in priority order, all handlers
        for an event are executed even if one fails.

        Args:
            event: Event to publish.
        """
        logger.debug(
            f'Publishing event: {event.type.name} from {event.source}')

        # Track history
        if self._history_enabled:
            self._history.append(event)
            if len(self._history) > self._max_history:
                self._history.pop(0)

        # Collect handlers
        handlers = self._handlers.get(event.type, []) + self._wildcard_handlers

        # Execute handlers
        for priority, handler in handlers:
            try:
                await handler(event)
            except Exception as e:
                logger.error(f'Event handler error for {event.type.name}: {e}')

    async def publish_async(self,
                            event: AgentEvent,
                            wait: bool = False) -> Optional[asyncio.Task]:
        """
        Publish event asynchronously.

        Args:
            event: Event to publish.
            wait: Whether to wait for handlers to complete.

        Returns:
            Task if not waiting, None otherwise.
        """
        if wait:
            await self.publish(event)
            return None
        else:
            task = asyncio.create_task(self.publish(event))
            return task

    def emit(self,
             event_type: EventType,
             data: Any = None,
             source: str = '',
             context: Optional['AgentContext'] = None) -> asyncio.Task:
        """
        Convenience method to create and publish an event.

        Args:
            event_type: Event type.
            data: Event data payload.
            source: Event source identifier.
            context: Optional agent context.

        Returns:
            Async task for event publication.
        """
        event = AgentEvent(
            type=event_type,
            data=data,
            source=source,
            context=context,
        )
        return asyncio.create_task(self.publish(event))

    def get_history(self,
                    event_type: Optional[EventType] = None,
                    limit: int = 10) -> List[AgentEvent]:
        """
        Get event history.

        Args:
            event_type: Filter by event type (None for all).
            limit: Maximum entries to return.

        Returns:
            List of recent events.
        """
        if event_type is None:
            return self._history[-limit:]
        return [e for e in self._history if e.type == event_type][-limit:]

    def clear_history(self) -> None:
        """Clear event history."""
        self._history.clear()

    def clear_all(self) -> None:
        """Clear all handlers and history."""
        self._handlers.clear()
        self._wildcard_handlers.clear()
        self._history.clear()

    def get_handler_count(self, event_type: Optional[EventType] = None) -> int:
        """
        Get number of registered handlers.

        Args:
            event_type: Specific event type (None for total).

        Returns:
            Handler count.
        """
        if event_type is None:
            total = sum(len(h) for h in self._handlers.values())
            return total + len(self._wildcard_handlers)
        return len(self._handlers.get(event_type, []))
