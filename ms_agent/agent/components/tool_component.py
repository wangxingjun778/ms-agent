# Copyright (c) ModelScope Contributors. All rights reserved.
"""
Tool component for external tool execution.

Provides a component wrapper around ToolManager for use in the
pipeline architecture.
"""
import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ms_agent.agent.core.component import AgentComponent
from ms_agent.agent.core.context import AgentContext
from ms_agent.tools import ToolManager
from ms_agent.utils.logger import get_logger
from omegaconf import DictConfig

logger = get_logger()


@dataclass
class ToolCallResult:
    """Result of a tool call."""
    tool_name: str
    tool_call_id: str
    result: Any
    success: bool = True
    error: Optional[str] = None


class ToolComponent(AgentComponent):
    """
    Component for tool management and execution.

    Wraps ToolManager and provides:
    - Tool registration and discovery
    - Parallel tool execution
    - Result formatting

    Attributes:
        tool_manager: Underlying ToolManager instance.
        mcp_config: MCP server configuration.
    """

    COMPONENT_NAME = 'tools'

    def __init__(self,
                 config: Optional[DictConfig] = None,
                 mcp_config: Optional[Dict[str, Any]] = None,
                 mcp_client: Any = None,
                 trust_remote_code: bool = False):
        """
        Initialize tool component.

        Args:
            config: Tool configuration.
            mcp_config: MCP server configuration.
            mcp_client: Pre-configured MCP client.
            trust_remote_code: Whether to trust remote code.
        """
        super().__init__(config)
        self._tool_manager: Optional[ToolManager] = None
        self._mcp_config = mcp_config or {}
        self._mcp_client = mcp_client
        self._trust_remote_code = trust_remote_code

    @property
    def name(self) -> str:
        """Component name."""
        return self.COMPONENT_NAME

    @property
    def tool_manager(self) -> Optional[ToolManager]:
        """Get underlying ToolManager instance."""
        return self._tool_manager

    async def initialize(self, context: AgentContext) -> None:
        """
        Initialize tool manager from configuration.

        Args:
            context: Agent context with configuration.
        """
        config = self._config or context.config

        self._tool_manager = ToolManager(
            config=config,
            mcp_config=self._mcp_config,
            mcp_client=self._mcp_client,
            trust_remote_code=self._trust_remote_code,
        )

        # Connect to MCP servers
        await self._tool_manager.connect()

        logger.info('Tool component initialized')
        self._mark_initialized()

    async def cleanup(self) -> None:
        """Cleanup tool manager resources."""
        if self._tool_manager:
            await self._tool_manager.cleanup()
        self._mark_cleanup()

    async def get_tools(self) -> List[Dict[str, Any]]:
        """
        Get available tool definitions for LLM.

        Returns:
            List of tool definition dictionaries.
        """
        if not self._tool_manager:
            return []
        return await self._tool_manager.get_tools()

    async def execute_tool(self, tool_name: str, tool_call_id: str,
                           arguments: Dict[str, Any]) -> ToolCallResult:
        """
        Execute a single tool call.

        Args:
            tool_name: Name of the tool to execute.
            tool_call_id: Unique identifier for this call.
            arguments: Tool arguments.

        Returns:
            ToolCallResult with execution outcome.
        """
        if not self._tool_manager:
            return ToolCallResult(
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                result=None,
                success=False,
                error='Tool manager not initialized')

        try:
            result = await self._tool_manager.call_tool(
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                arguments=arguments)
            return ToolCallResult(
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                result=result,
                success=True)
        except Exception as e:
            logger.error(f'Tool execution error [{tool_name}]: {e}')
            return ToolCallResult(
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                result=None,
                success=False,
                error=str(e))

    async def execute_parallel(
            self, tool_calls: List[Dict[str, Any]]) -> List[ToolCallResult]:
        """
        Execute multiple tool calls in parallel.

        Args:
            tool_calls: List of tool call dictionaries with:
                - tool_name: Tool name
                - id: Call identifier
                - arguments: Tool arguments

        Returns:
            List of ToolCallResult for each call.
        """
        if not self._tool_manager:
            return [
                ToolCallResult(
                    tool_name=tc.get('tool_name', ''),
                    tool_call_id=tc.get('id', ''),
                    result=None,
                    success=False,
                    error='Tool manager not initialized') for tc in tool_calls
            ]

        tasks = [
            self.execute_tool(
                tool_name=tc.get('tool_name', ''),
                tool_call_id=tc.get('id', ''),
                arguments=tc.get('arguments', {})) for tc in tool_calls
        ]

        return await asyncio.gather(*tasks)

    async def call_tool_raw(self, tool_calls: List[Dict[str,
                                                        Any]]) -> List[Any]:
        """
        Execute tool calls and return raw results.

        Compatibility method for existing code.

        Args:
            tool_calls: List of tool call dictionaries.

        Returns:
            List of raw tool results.
        """
        if not self._tool_manager:
            return []
        return await self._tool_manager.parallel_call_tool(tool_calls)

    def has_tool(self, tool_name: str) -> bool:
        """
        Check if a tool is available.

        Args:
            tool_name: Tool name to check.

        Returns:
            True if tool is available.
        """
        if not self._tool_manager:
            return False
        return self._tool_manager.has_tool(tool_name)
