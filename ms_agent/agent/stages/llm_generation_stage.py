# Copyright (c) ModelScope Contributors. All rights reserved.
"""
LLM generation pipeline stage.

Handles LLM response generation including tool calling support.
"""
import sys
from copy import deepcopy
from typing import Optional

from ms_agent.agent.components.llm_component import LLMComponent
from ms_agent.agent.components.tool_component import ToolComponent
from ms_agent.agent.core.context import AgentContext, ExecutionPhase
from ms_agent.agent.core.pipeline import NextStage, PipelineStage
from ms_agent.utils.logger import get_logger

logger = get_logger()


class LLMGenerationStage(PipelineStage):
    """
    Pipeline stage for LLM response generation.

    Responsibilities:
    - Get available tools from tool component
    - Generate LLM response (streaming or non-streaming)
    - Handle response with tool calls
    - Append response to message history

    Attributes:
        stream_to_stdout: Whether to stream output to stdout.
    """

    def __init__(self, stream_to_stdout: bool = True, enabled: bool = True):
        """
        Initialize stage.

        Args:
            stream_to_stdout: Stream LLM output to stdout.
            enabled: Whether stage is active.
        """
        super().__init__(enabled=enabled, name='LLMGenerationStage')
        self._stream_to_stdout = stream_to_stdout

    @property
    def order(self) -> int:
        """Execution order (runs after memory)."""
        return 30

    async def process(self, context: AgentContext,
                      next_stage: NextStage) -> AgentContext:
        """
        Generate LLM response.

        - Gets available tools
        - Generates response (streaming or not)
        - Handles tool call responses
        - Appends response to messages

        Args:
            context: Agent context.
            next_stage: Next stage callable.

        Returns:
            Context with LLM response appended.
        """
        context.state.phase = ExecutionPhase.LLM_GENERATION

        # Get components
        llm: LLMComponent = context.get_component('llm')
        tools: Optional[ToolComponent] = context.get_component('tools')

        if not llm or not llm.is_initialized:
            logger.error('LLM component not available')
            return await next_stage(context)

        # Get tool definitions
        tool_defs = []
        if tools and tools.is_initialized:
            tool_defs = await tools.get_tools()

        # Generate response
        if llm.stream_enabled:
            response = await self._generate_stream(context, llm, tool_defs)
        else:
            response = await self._generate_normal(context, llm, tool_defs)

        if response is None:
            logger.error('No response generated from LLM')
            return await next_stage(context)

        # Handle response
        self._handle_response(context, response)

        # Continue to next stage
        return await next_stage(context)

    async def _generate_normal(self, context: AgentContext, llm: LLMComponent,
                               tools: list):
        """Generate non-streaming response."""
        messages = deepcopy(context.messages)
        response = await llm.async_generate(messages, tools=tools)

        if response and response.content:
            self._log_output(context.tag, '[assistant]:')
            self._log_output(context.tag, response.content)

        return response

    async def _generate_stream(self, context: AgentContext, llm: LLMComponent,
                               tools: list):
        """Generate streaming response."""
        messages = deepcopy(context.messages)
        response = None
        content = ''

        self._log_output(context.tag, '[assistant]:')

        for chunk in llm.generate_stream(messages, tools=tools):
            response = chunk
            if chunk.content:
                new_content = chunk.content[len(content):]
                if self._stream_to_stdout:
                    sys.stdout.write(new_content)
                    sys.stdout.flush()
                content = chunk.content

        if self._stream_to_stdout:
            sys.stdout.write('\n')

        return response

    def _handle_response(self, context: AgentContext, response) -> None:
        """
        Handle LLM response and update context.

        Args:
            context: Agent context.
            response: LLM response message.
        """
        # Log tool calls if present
        if response.tool_calls:
            self._log_output(context.tag, '[tool_calling]:')
            for tool_call in response.tool_calls:
                import json
                tc = deepcopy(tool_call)
                if isinstance(tc.get('arguments'), str):
                    try:
                        tc['arguments'] = json.loads(tc['arguments'])
                    except json.JSONDecodeError:
                        pass
                self._log_output(context.tag,
                                 json.dumps(tc, ensure_ascii=False, indent=4))

        # Append to messages if not already there
        if context.messages[-1] is not response:
            context.messages.append(response)

        # Set placeholder content for tool-only responses
        if (context.messages[-1].role == 'assistant'
                and not context.messages[-1].content and response.tool_calls):
            context.messages[-1].content = 'Let me do a tool calling.'

        # Check if we should stop (no tool calls = final answer)
        if response.role == 'assistant' and not response.tool_calls:
            context.state.should_stop = True

        # Store response for other stages
        context.set_result('llm_response', response)

    def _log_output(self, tag: str, content: str) -> None:
        """Log formatted output."""
        if len(content) > 1024:
            content = content[:512] + '\n...\n' + content[-512:]
        for line in content.split('\n'):
            for _line in line.split('\\n'):
                logger.info(f'[{tag}] {_line}')
