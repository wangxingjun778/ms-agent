# Copyright (c) ModelScope Contributors. All rights reserved.
"""
Modular agent implementation using the pipeline architecture.

This module provides ModularAgent, a configurable agent that uses
the new component-based, pipeline-driven architecture while maintaining
backward compatibility with the existing Agent interface.
"""
from typing import Any, AsyncGenerator, Dict, List, Optional, Union

from ms_agent.llm.utils import Message
from ms_agent.utils.logger import get_logger
from omegaconf import DictConfig

from .base import Agent
from .components import LLMComponent, MemoryComponent, ToolComponent
from .core import (AgentContext, AgentEventBus, AgentOrchestrator, EventType,
                   PipelineStage)
from .stages import (InitializationStage, LLMGenerationStage, MemoryStage,
                     OutputStage, ToolExecutionStage)

logger = get_logger()


class ModularAgent(Agent):
    """
    Modular agent using pipeline-based architecture.

    This agent provides the same interface as LLMAgent but uses the new
    modular architecture internally. It supports:
    - Component-based design (LLM, Tools, Memory)
    - Pipeline-based execution flow
    - Event-driven callbacks
    - Easy extensibility via custom stages

    Example:
        ```python
        agent = ModularAgent(config, tag='my-agent')

        # Add custom stage
        agent.add_stage(MyCustomStage())

        # Subscribe to events
        agent.subscribe(EventType.LLM_GENERATE_END, my_handler)

        # Run
        result = await agent.run('Hello, world!')
        ```

    Attributes:
        orchestrator: Internal AgentOrchestrator instance.
    """

    AGENT_NAME = 'ModularAgent'

    def __init__(self,
                 config: DictConfig = DictConfig({}),
                 tag: str = 'modular',
                 trust_remote_code: bool = False,
                 mcp_config: Optional[Dict[str, Any]] = None,
                 mcp_client: Any = None,
                 **kwargs):
        """
        Initialize modular agent.

        Args:
            config: Agent configuration.
            tag: Agent identifier tag.
            trust_remote_code: Whether to trust external code.
            mcp_config: MCP server configuration.
            mcp_client: Pre-configured MCP client.
            **kwargs: Additional arguments.
        """
        super().__init__(config, tag, trust_remote_code)

        # Create orchestrator
        self.orchestrator = AgentOrchestrator(config=config, tag=tag)

        # Register default components
        self._register_default_components(
            mcp_config=mcp_config,
            mcp_client=mcp_client,
            trust_remote_code=trust_remote_code)

        # Build default pipeline
        self._build_default_pipeline()

    def _register_default_components(self,
                                     mcp_config: Optional[Dict[str,
                                                               Any]] = None,
                                     mcp_client: Any = None,
                                     trust_remote_code: bool = False) -> None:
        """Register default components."""
        # LLM component
        self.orchestrator.register_component(LLMComponent(self.config))

        # Tool component
        self.orchestrator.register_component(
            ToolComponent(
                config=self.config,
                mcp_config=mcp_config or {},
                mcp_client=mcp_client,
                trust_remote_code=trust_remote_code))

        # Memory component
        if hasattr(self.config, 'memory') and self.config.memory:
            self.orchestrator.register_component(MemoryComponent(self.config))

    def _build_default_pipeline(self) -> None:
        """Build default pipeline with standard stages."""
        self.orchestrator.add_stages([
            InitializationStage(),
            MemoryStage(),
            LLMGenerationStage(),
            ToolExecutionStage(),
            OutputStage(),
        ])

    def add_stage(self, stage: PipelineStage) -> 'ModularAgent':
        """
        Add a custom pipeline stage.

        Args:
            stage: Pipeline stage to add.

        Returns:
            Self for chaining.
        """
        self.orchestrator.add_stage(stage)
        return self

    def remove_stage(self, name: str) -> bool:
        """
        Remove a pipeline stage by name.

        Args:
            name: Stage name.

        Returns:
            True if removed.
        """
        return self.orchestrator.pipeline.remove_stage(name)

    def subscribe(self,
                  event_type: EventType,
                  handler: Any,
                  priority: int = 0) -> 'ModularAgent':
        """
        Subscribe to an event type.

        Args:
            event_type: Event type.
            handler: Event handler.
            priority: Handler priority.

        Returns:
            Self for chaining.
        """
        self.orchestrator.subscribe(event_type, handler, priority)
        return self

    @property
    def event_bus(self) -> AgentEventBus:
        """Get event bus."""
        return self.orchestrator.event_bus

    async def run(
            self, messages: Union[str, List[Message]], **kwargs
    ) -> Union[List[Message], AsyncGenerator[List[Message], Any]]:
        """
        Run the agent with given input.

        Args:
            messages: User query or message history.
            **kwargs: Additional arguments (stream, max_rounds, etc.).

        Returns:
            Final message history or async generator for streaming.
        """
        stream = kwargs.get('stream', False)
        max_rounds = kwargs.get('max_rounds', 20)

        if stream:
            return self._run_stream(messages, max_rounds)
        else:
            context = await self.orchestrator.run(
                query=messages if isinstance(messages, str) else messages,
                max_rounds=max_rounds)
            return context.messages

    async def _run_stream(
            self, messages: Union[str, List[Message]],
            max_rounds: int) -> AsyncGenerator[List[Message], Any]:
        """Run with streaming output."""
        async for context in self.orchestrator.run_stream(
                query=messages if isinstance(messages, str) else messages,
                max_rounds=max_rounds):
            yield context.messages


class AgentFactory:
    """
    Factory for creating configured agent instances.

    Provides convenient methods for creating agents with
    pre-configured components and stages.

    Example:
        ```python
        agent = AgentFactory.create_basic_agent(config)
        agent = AgentFactory.create_tool_agent(config, mcp_config)
        agent = AgentFactory.create_custom(config, components, stages)
        ```
    """

    @staticmethod
    def create_basic_agent(config: DictConfig,
                           tag: str = 'basic') -> ModularAgent:
        """
        Create a basic agent with LLM only.

        Args:
            config: Agent configuration.
            tag: Agent tag.

        Returns:
            Configured ModularAgent.
        """
        agent = ModularAgent(config, tag)
        # Remove tool and memory stages for basic agent
        agent.remove_stage('MemoryStage')
        agent.remove_stage('ToolExecutionStage')
        return agent

    @staticmethod
    def create_tool_agent(config: DictConfig,
                          tag: str = 'tool',
                          mcp_config: Optional[Dict[str, Any]] = None,
                          mcp_client: Any = None,
                          trust_remote_code: bool = False) -> ModularAgent:
        """
        Create an agent with tool calling support.

        Args:
            config: Agent configuration.
            tag: Agent tag.
            mcp_config: MCP server configuration.
            mcp_client: Pre-configured MCP client.
            trust_remote_code: Whether to trust external code.

        Returns:
            Configured ModularAgent.
        """
        return ModularAgent(
            config=config,
            tag=tag,
            trust_remote_code=trust_remote_code,
            mcp_config=mcp_config,
            mcp_client=mcp_client)

    @staticmethod
    def create_custom(config: DictConfig,
                      tag: str = 'custom',
                      components: Optional[List[Any]] = None,
                      stages: Optional[List[PipelineStage]] = None,
                      **kwargs) -> AgentOrchestrator:
        """
        Create a custom orchestrator with specified components and stages.

        Args:
            config: Agent configuration.
            tag: Agent tag.
            components: List of components to register.
            stages: List of pipeline stages.
            **kwargs: Additional orchestrator arguments.

        Returns:
            Configured AgentOrchestrator.
        """
        orchestrator = AgentOrchestrator(config=config, tag=tag)

        if components:
            orchestrator.register_components(components)

        if stages:
            orchestrator.add_stages(stages)

        return orchestrator
