# Copyright (c) ModelScope Contributors. All rights reserved.
"""
Memory pipeline stage.

Handles memory retrieval and message condensation for context management.
"""
from ms_agent.agent.components.memory_component import MemoryComponent
from ms_agent.agent.core.context import AgentContext, ExecutionPhase
from ms_agent.agent.core.pipeline import NextStage, PipelineStage
from ms_agent.utils.logger import get_logger

logger = get_logger()


class MemoryStage(PipelineStage):
    """
    Pipeline stage for memory processing.

    Responsibilities:
    - Condense messages to fit context window
    - Retrieve relevant memories for query augmentation
    - Manage memory state

    The stage integrates with MemoryComponent if available in context.
    """

    def __init__(self, enabled: bool = True):
        """
        Initialize stage.

        Args:
            enabled: Whether stage is active.
        """
        super().__init__(enabled=enabled, name='MemoryStage')

    @property
    def order(self) -> int:
        """Execution order (runs early)."""
        return 10

    async def process(self, context: AgentContext,
                      next_stage: NextStage) -> AgentContext:
        """
        Process memory operations.

        - Condenses messages if memory component available
        - Optionally retrieves relevant memories
        - Updates context with processed messages

        Args:
            context: Agent context.
            next_stage: Next stage callable.

        Returns:
            Processed context.
        """
        context.state.phase = ExecutionPhase.MEMORY_RETRIEVAL

        # Get memory component
        memory: MemoryComponent = context.get_component('memory')

        if memory and memory.is_initialized and memory.has_memory():
            logger.debug('Processing memory condensation')

            # Condense messages to fit context window
            condensed = await memory.condense(context.messages)
            context.messages = condensed

            logger.debug(
                f'Memory condensation complete, messages: {len(context.messages)}'
            )

        # Continue to next stage
        return await next_stage(context)
