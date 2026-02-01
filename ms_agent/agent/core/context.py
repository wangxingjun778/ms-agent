# Copyright (c) ModelScope Contributors. All rights reserved.
"""
Agent context module providing unified execution context.

The AgentContext encapsulates all state and component references
needed during agent execution, flowing through the pipeline stages.
"""
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from ms_agent.llm.utils import Message


class ExecutionPhase(Enum):
    """Execution phase enumeration."""
    INITIALIZING = auto()
    MEMORY_RETRIEVAL = auto()
    RAG_AUGMENTATION = auto()
    LLM_GENERATION = auto()
    TOOL_EXECUTION = auto()
    SKILL_EXECUTION = auto()
    OUTPUT_PROCESSING = auto()
    COMPLETED = auto()
    ERROR = auto()


@dataclass
class AgentState:
    """
    Agent execution state container.

    Centralizes all mutable state for the agent execution,
    making state management explicit and traceable.

    Attributes:
        should_stop: Flag indicating execution should terminate.
        current_round: Current execution round number.
        max_rounds: Maximum allowed execution rounds.
        phase: Current execution phase.
        error: Error information if execution failed.
        metadata: Additional state metadata.
    """
    should_stop: bool = False
    current_round: int = 0
    max_rounds: int = 20
    phase: ExecutionPhase = ExecutionPhase.INITIALIZING
    error: Optional[Exception] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def increment_round(self) -> int:
        """Increment round counter and return new value."""
        self.current_round += 1
        return self.current_round

    def mark_complete(self) -> None:
        """Mark execution as complete."""
        self.should_stop = True
        self.phase = ExecutionPhase.COMPLETED

    def mark_error(self, error: Exception) -> None:
        """Mark execution as failed with error."""
        self.error = error
        self.phase = ExecutionPhase.ERROR
        self.should_stop = True

    def is_max_rounds_reached(self) -> bool:
        """Check if maximum rounds exceeded."""
        return self.current_round >= self.max_rounds

    def to_dict(self) -> Dict[str, Any]:
        """Serialize state to dictionary."""
        return {
            'should_stop': self.should_stop,
            'current_round': self.current_round,
            'max_rounds': self.max_rounds,
            'phase': self.phase.name,
            'metadata': self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'AgentState':
        """Deserialize state from dictionary."""
        state = cls(
            should_stop=data.get('should_stop', False),
            current_round=data.get('current_round', 0),
            max_rounds=data.get('max_rounds', 20),
            metadata=data.get('metadata', {}),
        )
        phase_name = data.get('phase', 'INITIALIZING')
        state.phase = ExecutionPhase[phase_name]
        return state


@dataclass
class AgentContext:
    """
    Unified execution context for agent pipeline.

    This context flows through all pipeline stages, carrying:
    - Message history
    - Execution state
    - Component references
    - Intermediate results

    The context is immutable by convention; stages should clone
    before making modifications to support rollback/retry.

    Attributes:
        messages: Conversation message history.
        state: Execution state container.
        tag: Agent identifier tag.
        user_query: Original user query string.
        components: Registered component instances.
        results: Intermediate results from stages.
        config: Agent configuration.
    """
    messages: List['Message'] = field(default_factory=list)
    state: AgentState = field(default_factory=AgentState)
    tag: str = 'default'
    user_query: str = ''

    # Component references (populated by orchestrator)
    components: Dict[str, Any] = field(default_factory=dict)

    # Intermediate results from pipeline stages
    results: Dict[str, Any] = field(default_factory=dict)

    # Configuration reference
    config: Optional[Any] = None

    def get_component(self, name: str) -> Optional[Any]:
        """
        Get a registered component by name.

        Args:
            name: Component name.

        Returns:
            Component instance or None if not found.
        """
        return self.components.get(name)

    def set_result(self, key: str, value: Any) -> None:
        """
        Store an intermediate result.

        Args:
            key: Result identifier.
            value: Result value.
        """
        self.results[key] = value

    def get_result(self, key: str, default: Any = None) -> Any:
        """
        Retrieve an intermediate result.

        Args:
            key: Result identifier.
            default: Default value if not found.

        Returns:
            Result value or default.
        """
        return self.results.get(key, default)

    def get_last_message(self) -> Optional['Message']:
        """Get the most recent message."""
        return self.messages[-1] if self.messages else None

    def get_last_user_message(self) -> Optional['Message']:
        """Get the most recent user message."""
        for msg in reversed(self.messages):
            if msg.role == 'user':
                return msg
        return None

    def get_last_assistant_message(self) -> Optional['Message']:
        """Get the most recent assistant message."""
        for msg in reversed(self.messages):
            if msg.role == 'assistant':
                return msg
        return None

    def append_message(self, message: 'Message') -> None:
        """
        Append a message to history.

        Args:
            message: Message to append.
        """
        self.messages.append(message)

    def clone(self) -> 'AgentContext':
        """
        Create a deep copy of the context.

        Returns:
            Cloned context instance.
        """
        return AgentContext(
            messages=deepcopy(self.messages),
            state=AgentState(
                should_stop=self.state.should_stop,
                current_round=self.state.current_round,
                max_rounds=self.state.max_rounds,
                phase=self.state.phase,
                error=self.state.error,
                metadata=deepcopy(self.state.metadata),
            ),
            tag=self.tag,
            user_query=self.user_query,
            components=self.components,  # Shallow copy - components are shared
            results=deepcopy(self.results),
            config=self.config,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize context to dictionary (excluding components)."""
        return {
            'messages': [
                m.to_dict() if hasattr(m, 'to_dict') else str(m)
                for m in self.messages
            ],
            'state':
            self.state.to_dict(),
            'tag':
            self.tag,
            'user_query':
            self.user_query,
            'results':
            self.results,
        }
