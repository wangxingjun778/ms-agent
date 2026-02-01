# Copyright (c) ModelScope Contributors. All rights reserved.
"""
LLM component for language model interaction.

Provides a component wrapper around the LLM for use in the
pipeline architecture.
"""
import asyncio
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Dict, List, Optional, Union

from ms_agent.agent.core.component import AgentComponent
from ms_agent.agent.core.context import AgentContext
from ms_agent.llm import LLM
from ms_agent.llm.utils import Message
from ms_agent.utils.logger import get_logger
from omegaconf import DictConfig

logger = get_logger()


@dataclass
class TokenUsage:
    """Token usage statistics."""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def add(self, prompt: int, completion: int) -> None:
        """Add token counts."""
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.total_tokens += prompt + completion


class LLMComponent(AgentComponent):
    """
    Component for language model interaction.

    Wraps an LLM instance and provides:
    - Message generation (streaming and non-streaming)
    - Token usage tracking
    - Response handling

    Attributes:
        llm: Underlying LLM instance.
        token_usage: Accumulated token statistics.
        stream_enabled: Whether streaming is enabled.
    """

    COMPONENT_NAME = 'llm'

    def __init__(self,
                 config: Optional[DictConfig] = None,
                 llm: Optional[LLM] = None):
        """
        Initialize LLM component.

        Args:
            config: LLM configuration (used if llm not provided).
            llm: Pre-configured LLM instance.
        """
        super().__init__(config)
        self._llm = llm
        self._token_usage = TokenUsage()
        self._stream_enabled = False

    @property
    def name(self) -> str:
        """Component name."""
        return self.COMPONENT_NAME

    @property
    def llm(self) -> Optional[LLM]:
        """Get underlying LLM instance."""
        return self._llm

    @property
    def token_usage(self) -> TokenUsage:
        """Get token usage statistics."""
        return self._token_usage

    @property
    def stream_enabled(self) -> bool:
        """Check if streaming is enabled."""
        return self._stream_enabled

    async def initialize(self, context: AgentContext) -> None:
        """
        Initialize LLM from configuration.

        Args:
            context: Agent context with configuration.
        """
        if self._llm is not None:
            self._mark_initialized()
            return

        # Create LLM from config
        config = self._config or context.config
        if config and hasattr(config, 'llm'):
            self._llm = LLM.from_config(config)
            logger.info('LLM component initialized from config')
        else:
            logger.warning('No LLM configuration found')

        # Check stream setting
        gen_config = getattr(config, 'generation_config', DictConfig({}))
        self._stream_enabled = getattr(gen_config, 'stream', False)

        self._mark_initialized()

    async def cleanup(self) -> None:
        """Cleanup LLM resources."""
        # LLM doesn't typically need cleanup
        self._mark_cleanup()

    def generate(self,
                 messages: List[Message],
                 tools: Optional[List[Dict[str, Any]]] = None,
                 **kwargs) -> Message:
        """
        Generate a response from the LLM.

        Args:
            messages: Conversation message history.
            tools: Optional list of tool definitions.
            **kwargs: Additional generation parameters.

        Returns:
            Generated message response.

        Raises:
            RuntimeError: If LLM not initialized.
        """
        if not self._llm:
            raise RuntimeError('LLM not initialized')

        response = self._llm.generate(messages, tools=tools, **kwargs)

        # Track token usage
        if hasattr(response, 'prompt_tokens') and hasattr(
                response, 'completion_tokens'):
            self._token_usage.add(response.prompt_tokens or 0,
                                  response.completion_tokens or 0)

        return response

    def generate_stream(self,
                        messages: List[Message],
                        tools: Optional[List[Dict[str, Any]]] = None,
                        **kwargs) -> AsyncGenerator[Message, None]:
        """
        Generate a streaming response from the LLM.

        Args:
            messages: Conversation message history.
            tools: Optional list of tool definitions.
            **kwargs: Additional generation parameters.

        Yields:
            Message chunks as they are generated.

        Raises:
            RuntimeError: If LLM not initialized.
        """
        if not self._llm:
            raise RuntimeError('LLM not initialized')

        for chunk in self._llm.generate(messages, tools=tools, **kwargs):
            yield chunk

    async def async_generate(self,
                             messages: List[Message],
                             tools: Optional[List[Dict[str, Any]]] = None,
                             **kwargs) -> Message:
        """
        Async wrapper for generate.

        Args:
            messages: Conversation message history.
            tools: Optional list of tool definitions.
            **kwargs: Additional generation parameters.

        Returns:
            Generated message response.
        """
        return await asyncio.to_thread(self.generate, messages, tools,
                                       **kwargs)

    def reset_token_usage(self) -> None:
        """Reset token usage counters."""
        self._token_usage = TokenUsage()

    def get_token_summary(self) -> Dict[str, int]:
        """Get token usage summary."""
        return {
            'prompt_tokens': self._token_usage.prompt_tokens,
            'completion_tokens': self._token_usage.completion_tokens,
            'total_tokens': self._token_usage.total_tokens,
        }
