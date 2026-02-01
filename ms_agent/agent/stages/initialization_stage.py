# Copyright (c) ModelScope Contributors. All rights reserved.
"""
Initialization pipeline stage.

Handles context preparation, message normalization, and initial setup
before main processing begins.
"""
from typing import Optional

from ms_agent.agent.core.context import AgentContext, ExecutionPhase
from ms_agent.agent.core.pipeline import NextStage, PipelineStage
from ms_agent.llm.utils import Message
from ms_agent.utils.logger import get_logger

logger = get_logger()


class InitializationStage(PipelineStage):
    """
    Pipeline stage for context initialization.

    Responsibilities:
    - Ensure system message is present
    - Normalize message format
    - Set execution phase
    - Log round information

    Attributes:
        default_system: Default system prompt if none provided.
    """

    DEFAULT_SYSTEM = 'You are a helpful assistant.'

    def __init__(self,
                 default_system: Optional[str] = None,
                 enabled: bool = True):
        """
        Initialize stage.

        Args:
            default_system: Default system prompt.
            enabled: Whether stage is active.
        """
        super().__init__(enabled=enabled, name='InitializationStage')
        self._default_system = default_system or self.DEFAULT_SYSTEM

    @property
    def order(self) -> int:
        """Execution order (runs first)."""
        return 0

    async def process(self, context: AgentContext,
                      next_stage: NextStage) -> AgentContext:
        """
        Initialize context for pipeline execution.

        - Ensures messages list has system message
        - Logs current round information
        - Updates execution phase

        Args:
            context: Agent context.
            next_stage: Next stage callable.

        Returns:
            Processed context.
        """
        # Set phase
        context.state.phase = ExecutionPhase.INITIALIZING

        # Ensure messages list exists
        if context.messages is None:
            context.messages = []

        # Add system message if not present
        if not context.messages or context.messages[0].role != 'system':
            system_prompt = self._get_system_prompt(context)
            context.messages.insert(
                0, Message(role='system', content=system_prompt))
        elif context.config:
            # Update system message from config if different
            config_system = self._get_system_prompt(context)
            if config_system and context.messages[0].content != config_system:
                context.messages[0].content = config_system

        # Log round info
        logger.info(
            f'[{context.tag}] Round {context.state.current_round} starting, '
            f'messages: {len(context.messages)}')

        # Continue to next stage
        return await next_stage(context)

    def _get_system_prompt(self, context: AgentContext) -> str:
        """
        Get system prompt from config or default.

        Args:
            context: Agent context.

        Returns:
            System prompt string.
        """
        if context.config and hasattr(context.config, 'prompt'):
            system = getattr(context.config.prompt, 'system', None)
            if system:
                return system
        return self._default_system
