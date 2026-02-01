# Copyright (c) ModelScope Contributors. All rights reserved.
"""
Agent orchestrator module for coordinating execution.

The orchestrator manages components, pipeline execution, and events,
providing the main entry point for running agent tasks.
"""
import asyncio
from typing import TYPE_CHECKING, Any, AsyncGenerator, List, Optional, Union

from ms_agent.utils.logger import get_logger
from omegaconf import DictConfig

from .component import AgentComponent, ComponentRegistry
from .context import AgentContext, AgentState, ExecutionPhase
from .event_bus import AgentEvent, AgentEventBus, EventType
from .pipeline import Pipeline, PipelineStage

if TYPE_CHECKING:
    from ms_agent.llm.utils import Message

logger = get_logger()


class AgentOrchestrator:
    """
    Central coordinator for agent execution.

    The orchestrator manages:
    - Component registration and lifecycle
    - Pipeline construction and execution
    - Event publication and subscription
    - Context creation and management

    Provides a high-level API for running agent tasks with full
    control over components, stages, and events.

    Attributes:
        components: Component registry.
        pipeline: Execution pipeline.
        event_bus: Event publication bus.
        config: Agent configuration.
    """

    def __init__(self,
                 config: Optional[DictConfig] = None,
                 tag: str = 'orchestrator'):
        """
        Initialize orchestrator.

        Args:
            config: Agent configuration.
            tag: Orchestrator identifier tag.
        """
        self.config = config or DictConfig({})
        self.tag = tag
        self.components = ComponentRegistry()
        self.pipeline = Pipeline()
        self.event_bus = AgentEventBus()

        # Internal state
        self._initialized = False
        self._running = False

    def register_component(self,
                           component: AgentComponent) -> 'AgentOrchestrator':
        """
        Register a component with the orchestrator.

        Args:
            component: Component instance to register.

        Returns:
            Self for chaining.
        """
        self.components.register(component)
        return self

    def register_components(
            self, components: List[AgentComponent]) -> 'AgentOrchestrator':
        """
        Register multiple components.

        Args:
            components: List of components to register.

        Returns:
            Self for chaining.
        """
        for component in components:
            self.register_component(component)
        return self

    def add_stage(self, stage: PipelineStage) -> 'AgentOrchestrator':
        """
        Add a pipeline stage.

        Args:
            stage: Pipeline stage to add.

        Returns:
            Self for chaining.
        """
        self.pipeline.add_stage(stage)
        return self

    def add_stages(self, stages: List[PipelineStage]) -> 'AgentOrchestrator':
        """
        Add multiple pipeline stages.

        Args:
            stages: List of stages to add.

        Returns:
            Self for chaining.
        """
        self.pipeline.add_stages(stages)
        return self

    def subscribe(self,
                  event_type: EventType,
                  handler: Any,
                  priority: int = 0) -> 'AgentOrchestrator':
        """
        Subscribe to an event type.

        Args:
            event_type: Event type to subscribe to.
            handler: Event handler function.
            priority: Handler priority.

        Returns:
            Self for chaining.
        """
        self.event_bus.subscribe(event_type, handler, priority)
        return self

    def create_context(self,
                       messages: Optional[List['Message']] = None,
                       query: Optional[str] = None,
                       max_rounds: int = 20,
                       **kwargs) -> AgentContext:
        """
        Create a new execution context.

        Args:
            messages: Initial message history.
            query: User query string.
            max_rounds: Maximum execution rounds.
            **kwargs: Additional context metadata.

        Returns:
            New AgentContext instance.
        """
        from ms_agent.llm.utils import Message

        # Build initial messages
        if messages is None:
            messages = []
            # Add system message if configured
            system_prompt = self.config.get('prompt', {}).get('system')
            if system_prompt:
                messages.append(Message(role='system', content=system_prompt))
            # Add user query
            if query:
                messages.append(Message(role='user', content=query))

        # Create state
        state = AgentState(
            max_rounds=max_rounds,
            metadata=kwargs,
        )

        # Create context
        context = AgentContext(
            messages=messages,
            state=state,
            tag=self.tag,
            user_query=query or '',
            config=self.config,
        )

        # Inject component references
        for name in self.components.get_names():
            context.components[name] = self.components.get(name)

        return context

    async def initialize(self, context: AgentContext) -> None:
        """
        Initialize all components with context.

        Args:
            context: Agent context for initialization.
        """
        if self._initialized:
            return

        logger.info(f'Initializing orchestrator [{self.tag}]')

        # Initialize all components
        await self.components.initialize_all(context)

        # Publish initialization event
        await self.event_bus.publish(
            AgentEvent(
                type=EventType.TASK_BEGIN,
                source=self.tag,
                context=context,
            ))

        self._initialized = True

    async def cleanup(self) -> None:
        """Cleanup all components and resources."""
        if not self._initialized:
            return

        logger.info(f'Cleaning up orchestrator [{self.tag}]')

        # Cleanup all components
        await self.components.cleanup_all()

        self._initialized = False

    async def execute(self, context: AgentContext) -> AgentContext:
        """
        Execute the pipeline with given context.

        Single execution pass through all pipeline stages.

        Args:
            context: Execution context.

        Returns:
            Processed context after pipeline execution.
        """
        if not self._initialized:
            await self.initialize(context)

        context.state.phase = ExecutionPhase.INITIALIZING
        return await self.pipeline.execute(context)

    async def run(self,
                  query: Union[str, List['Message']],
                  max_rounds: int = 20,
                  stop_on_complete: bool = True,
                  **kwargs) -> AgentContext:
        """
        Run the agent with given input.

        Executes the pipeline in a loop until:
        - state.should_stop is True
        - max_rounds is reached
        - An error occurs

        Args:
            query: User query string or message list.
            max_rounds: Maximum execution rounds.
            stop_on_complete: Stop when LLM provides final answer.
            **kwargs: Additional context arguments.

        Returns:
            Final execution context.
        """
        # Create context
        if isinstance(query, list):
            context = self.create_context(
                messages=query, max_rounds=max_rounds, **kwargs)
        else:
            context = self.create_context(
                query=query, max_rounds=max_rounds, **kwargs)

        try:
            self._running = True

            # Initialize
            await self.initialize(context)

            # Publish task begin
            await self.event_bus.publish(
                AgentEvent(
                    type=EventType.TASK_BEGIN,
                    source=self.tag,
                    context=context,
                ))

            # Execute loop
            while not context.state.should_stop:
                # Check round limit
                if context.state.is_max_rounds_reached():
                    logger.warning(
                        f'Max rounds ({max_rounds}) reached, stopping')
                    context.state.mark_complete()
                    break

                # Publish round begin
                await self.event_bus.publish(
                    AgentEvent(
                        type=EventType.ROUND_BEGIN,
                        data={'round': context.state.current_round},
                        source=self.tag,
                        context=context,
                    ))

                # Execute pipeline
                context = await self.execute(context)

                # Publish round end
                await self.event_bus.publish(
                    AgentEvent(
                        type=EventType.ROUND_END,
                        data={'round': context.state.current_round},
                        source=self.tag,
                        context=context,
                    ))

                # Increment round
                context.state.increment_round()

            # Publish task end
            await self.event_bus.publish(
                AgentEvent(
                    type=EventType.TASK_END,
                    source=self.tag,
                    context=context,
                ))

            return context

        except Exception as e:
            logger.error(f'Orchestrator execution error: {e}')
            context.state.mark_error(e)

            # Publish error event
            await self.event_bus.publish(
                AgentEvent(
                    type=EventType.ERROR,
                    data={'error': str(e)},
                    source=self.tag,
                    context=context,
                ))

            raise

        finally:
            self._running = False
            await self.cleanup()

    async def run_stream(self,
                         query: Union[str, List['Message']],
                         max_rounds: int = 20,
                         **kwargs) -> AsyncGenerator[AgentContext, None]:
        """
        Run agent with streaming output.

        Yields context after each pipeline execution round.

        Args:
            query: User query string or message list.
            max_rounds: Maximum execution rounds.
            **kwargs: Additional context arguments.

        Yields:
            Context after each round.
        """
        # Create context
        if isinstance(query, list):
            context = self.create_context(
                messages=query, max_rounds=max_rounds, **kwargs)
        else:
            context = self.create_context(
                query=query, max_rounds=max_rounds, **kwargs)

        try:
            self._running = True
            await self.initialize(context)

            while not context.state.should_stop:
                if context.state.is_max_rounds_reached():
                    context.state.mark_complete()
                    break

                context = await self.execute(context)
                yield context
                context.state.increment_round()

        except Exception as e:
            context.state.mark_error(e)
            yield context
            raise

        finally:
            self._running = False
            await self.cleanup()

    @property
    def is_running(self) -> bool:
        """Check if orchestrator is currently executing."""
        return self._running

    @property
    def is_initialized(self) -> bool:
        """Check if orchestrator is initialized."""
        return self._initialized

    def __repr__(self) -> str:
        """String representation."""
        components = ', '.join(self.components.get_names())
        return f'AgentOrchestrator(tag={self.tag}, components=[{components}])'
