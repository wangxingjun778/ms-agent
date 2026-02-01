# Copyright (c) ModelScope Contributors. All rights reserved.
"""
Pipeline module for agent execution flow.

The pipeline provides a middleware-like architecture where each stage
can process the context and optionally pass control to the next stage.
"""
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Callable, Coroutine, List, Optional

from ms_agent.utils.logger import get_logger

if TYPE_CHECKING:
    from .context import AgentContext

logger = get_logger()

# Type alias for next stage callable
NextStage = Callable[['AgentContext'], Coroutine[Any, Any, 'AgentContext']]


class PipelineStage(ABC):
    """
    Base class for pipeline stages.

    Pipeline stages process the agent context in sequence, implementing
    a middleware pattern where each stage can:
    - Pre-process before calling next stage
    - Call next stage and await result
    - Post-process after next stage completes
    - Short-circuit by not calling next stage

    Subclasses must implement the process() method.

    Attributes:
        _enabled: Whether the stage is enabled.
        _name: Optional custom stage name.
    """

    def __init__(self, enabled: bool = True, name: Optional[str] = None):
        """
        Initialize pipeline stage.

        Args:
            enabled: Whether the stage is active.
            name: Optional custom name override.
        """
        self._enabled = enabled
        self._name = name

    @property
    def name(self) -> str:
        """
        Stage name for logging and identification.

        Returns:
            Stage name string.
        """
        return self._name or self.__class__.__name__

    @property
    def order(self) -> int:
        """
        Execution order (lower = earlier).

        Override in subclasses to control ordering.

        Returns:
            Order value (default 0).
        """
        return 0

    @property
    def enabled(self) -> bool:
        """Check if stage is enabled."""
        return self._enabled

    def enable(self) -> None:
        """Enable the stage."""
        self._enabled = True

    def disable(self) -> None:
        """Disable the stage."""
        self._enabled = False

    @abstractmethod
    async def process(self, context: 'AgentContext',
                      next_stage: NextStage) -> 'AgentContext':
        """
        Process the context and optionally delegate to next stage.

        Middleware pattern implementation:
        ```python
        async def process(self, context, next_stage):
            # Pre-processing
            context = self.pre_process(context)

            # Delegate to next stage (or skip)
            if self.should_continue(context):
                context = await next_stage(context)

            # Post-processing
            context = self.post_process(context)

            return context
        ```

        Args:
            context: Current agent context.
            next_stage: Callable to invoke next stage.

        Returns:
            Processed agent context.
        """
        raise NotImplementedError()

    async def on_error(self, context: 'AgentContext',
                       error: Exception) -> 'AgentContext':
        """
        Handle errors during stage processing.

        Override to implement custom error handling.
        Default behavior marks context with error and stops execution.

        Args:
            context: Current agent context.
            error: Exception that occurred.

        Returns:
            Context with error state.
        """
        logger.error(f'Stage [{self.name}] error: {error}')
        context.state.mark_error(error)
        return context


class Pipeline:
    """
    Pipeline for executing stages in sequence.

    Manages stage ordering, execution flow, and error handling.
    Supports dynamic stage addition/removal and conditional execution.

    Attributes:
        _stages: Registered pipeline stages.
        _error_handler: Global error handler callable.
    """

    def __init__(self):
        """Initialize empty pipeline."""
        self._stages: List[PipelineStage] = []
        self._error_handler: Optional[Callable] = None

    def add_stage(self, stage: PipelineStage) -> 'Pipeline':
        """
        Add a stage to the pipeline.

        Stages are automatically sorted by order property.

        Args:
            stage: Pipeline stage to add.

        Returns:
            Self for chaining.
        """
        self._stages.append(stage)
        self._stages.sort(key=lambda s: s.order)
        logger.debug(
            f'Added pipeline stage: {stage.name} (order={stage.order})')
        return self

    def add_stages(self, stages: List[PipelineStage]) -> 'Pipeline':
        """
        Add multiple stages to the pipeline.

        Args:
            stages: List of stages to add.

        Returns:
            Self for chaining.
        """
        for stage in stages:
            self.add_stage(stage)
        return self

    def remove_stage(self, name: str) -> bool:
        """
        Remove a stage by name.

        Args:
            name: Stage name to remove.

        Returns:
            True if stage was removed, False if not found.
        """
        for i, stage in enumerate(self._stages):
            if stage.name == name:
                self._stages.pop(i)
                logger.debug(f'Removed pipeline stage: {name}')
                return True
        return False

    def get_stage(self, name: str) -> Optional[PipelineStage]:
        """
        Get a stage by name.

        Args:
            name: Stage name.

        Returns:
            Stage instance or None if not found.
        """
        for stage in self._stages:
            if stage.name == name:
                return stage
        return None

    def set_error_handler(
            self, handler: Callable[['AgentContext', Exception],
                                    Coroutine]) -> None:
        """
        Set global error handler for pipeline execution.

        Args:
            handler: Async callable (context, error) -> context.
        """
        self._error_handler = handler

    def _build_chain(self) -> NextStage:
        """
        Build the execution chain from stages.

        Creates a nested chain of callables where each stage
        wraps the next, enabling middleware-style execution.

        Returns:
            Root callable of the chain.
        """

        # Terminal stage - returns context as-is
        async def terminal(context: 'AgentContext') -> 'AgentContext':
            return context

        # Build chain in reverse order
        chain = terminal
        for stage in reversed(self._stages):
            if stage.enabled:
                # Capture stage and next in closure
                chain = self._wrap_stage(stage, chain)

        return chain

    def _wrap_stage(self, stage: PipelineStage,
                    next_stage: NextStage) -> NextStage:
        """
        Wrap a stage with error handling.

        Args:
            stage: Stage to wrap.
            next_stage: Next stage callable.

        Returns:
            Wrapped callable.
        """

        async def wrapped(context: 'AgentContext') -> 'AgentContext':
            try:
                logger.debug(f'Entering stage: {stage.name}')
                result = await stage.process(context, next_stage)
                logger.debug(f'Exiting stage: {stage.name}')
                return result
            except Exception as e:
                logger.error(f'Error in stage [{stage.name}]: {e}')
                if self._error_handler:
                    return await self._error_handler(context, e)
                return await stage.on_error(context, e)

        return wrapped

    async def execute(self, context: 'AgentContext') -> 'AgentContext':
        """
        Execute the pipeline with given context.

        Args:
            context: Initial agent context.

        Returns:
            Final processed context.
        """
        if not self._stages:
            logger.warning(
                'Pipeline has no stages, returning context unchanged')
            return context

        chain = self._build_chain()
        logger.debug(f'Executing pipeline with {len(self._stages)} stages')
        return await chain(context)

    def get_stage_names(self) -> List[str]:
        """Get ordered list of stage names."""
        return [s.name for s in self._stages]

    def __len__(self) -> int:
        """Get number of stages."""
        return len(self._stages)

    def __repr__(self) -> str:
        """String representation."""
        stages = ' -> '.join(self.get_stage_names())
        return f'Pipeline({stages})'
