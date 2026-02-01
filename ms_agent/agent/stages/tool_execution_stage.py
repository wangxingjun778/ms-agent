# Copyright (c) ModelScope Contributors. All rights reserved.
"""
Tool execution pipeline stage.

Handles parallel execution of tool calls from LLM response.
"""
import uuid
from typing import List, Optional

from ms_agent.agent.components.tool_component import ToolComponent
from ms_agent.agent.core.context import AgentContext, ExecutionPhase
from ms_agent.agent.core.pipeline import NextStage, PipelineStage
from ms_agent.llm.utils import Message, ToolResult
from ms_agent.utils.logger import get_logger

logger = get_logger()


class ToolExecutionStage(PipelineStage):
    """
    Pipeline stage for tool execution.

    Responsibilities:
    - Extract tool calls from last assistant message
    - Execute tools in parallel
    - Format and append tool results to messages

    This stage only processes if the last message has tool calls.
    """

    def __init__(self, enabled: bool = True):
        """
        Initialize stage.

        Args:
            enabled: Whether stage is active.
        """
        super().__init__(enabled=enabled, name='ToolExecutionStage')

    @property
    def order(self) -> int:
        """Execution order (runs after LLM generation)."""
        return 40

    async def process(self, context: AgentContext,
                      next_stage: NextStage) -> AgentContext:
        """
        Execute tool calls from LLM response.

        - Checks if last message has tool calls
        - Executes tools in parallel via ToolComponent
        - Appends tool results to messages

        Args:
            context: Agent context.
            next_stage: Next stage callable.

        Returns:
            Context with tool results appended.
        """
        context.state.phase = ExecutionPhase.TOOL_EXECUTION

        # Get tool component
        tools: Optional[ToolComponent] = context.get_component('tools')

        if not tools or not tools.is_initialized:
            return await next_stage(context)

        # Get LLM response
        response = context.get_result('llm_response')
        if not response or not response.tool_calls:
            return await next_stage(context)

        # Execute tools
        await self._execute_tool_calls(context, tools, response.tool_calls)

        # Continue to next stage
        return await next_stage(context)

    async def _execute_tool_calls(self, context: AgentContext,
                                  tools: ToolComponent,
                                  tool_calls: List[dict]) -> None:
        """
        Execute tool calls and append results to messages.

        Args:
            context: Agent context.
            tools: Tool component.
            tool_calls: List of tool call dictionaries.
        """
        logger.debug(f'Executing {len(tool_calls)} tool calls')

        # Execute in parallel
        raw_results = await tools.call_tool_raw(tool_calls)

        if len(raw_results) != len(tool_calls):
            logger.error(
                f'Tool result count mismatch: {len(raw_results)} vs {len(tool_calls)}'
            )
            return

        # Format and append results
        for result, call in zip(raw_results, tool_calls):
            tool_result = ToolResult.from_raw(result)

            # Ensure tool call has an ID
            tool_call_id = call.get('id')
            if tool_call_id is None:
                tool_call_id = str(uuid.uuid4())[:8]
                call['id'] = tool_call_id

            # Create tool result message
            message = Message(
                role='tool',
                content=tool_result.text,
                tool_call_id=tool_call_id,
                name=call.get('tool_name', ''),
                resources=tool_result.resources)

            context.messages.append(message)
            self._log_tool_result(context.tag, message)

    def _log_tool_result(self, tag: str, message: Message) -> None:
        """Log tool result content."""
        content = message.content
        if len(content) > 1024:
            content = content[:512] + '\n...\n' + content[-512:]
        for line in content.split('\n'):
            for _line in line.split('\\n'):
                logger.info(f'[{tag}] {_line}')
