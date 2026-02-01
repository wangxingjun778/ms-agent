# Copyright (c) ModelScope Contributors. All rights reserved.
"""
Memory component for conversation memory management.

Provides a component wrapper around Memory instances for use in the
pipeline architecture.
"""
from typing import Any, Dict, List, Optional

from ms_agent.agent.core.component import AgentComponent
from ms_agent.agent.core.context import AgentContext
from ms_agent.llm.utils import Message
from ms_agent.memory import Memory, memory_mapping
from ms_agent.memory.memory_manager import SharedMemoryManager
from ms_agent.utils.logger import get_logger
from omegaconf import DictConfig

logger = get_logger()


class MemoryComponent(AgentComponent):
    """
    Component for memory management.

    Provides memory retrieval, storage, and condensation capabilities.
    Supports multiple memory backends through the memory_mapping registry.

    Attributes:
        memory_instances: List of active memory instances.
    """

    COMPONENT_NAME = 'memory'

    def __init__(self, config: Optional[DictConfig] = None):
        """
        Initialize memory component.

        Args:
            config: Memory configuration.
        """
        super().__init__(config)
        self._memory_instances: List[Memory] = []

    @property
    def name(self) -> str:
        """Component name."""
        return self.COMPONENT_NAME

    @property
    def memory_instances(self) -> List[Memory]:
        """Get active memory instances."""
        return self._memory_instances

    async def initialize(self, context: AgentContext) -> None:
        """
        Initialize memory instances from configuration.

        Args:
            context: Agent context with configuration.
        """
        config = self._config or context.config

        if not config or not hasattr(config, 'memory'):
            logger.debug('No memory configuration found')
            self._mark_initialized()
            return

        # Load each configured memory type
        for mem_type, mem_config in config.memory.items():
            if mem_type not in memory_mapping:
                logger.warning(f'Unknown memory type: {mem_type}, '
                               f'available: {list(memory_mapping.keys())}')
                continue

            try:
                shared_memory = await SharedMemoryManager.get_shared_memory(
                    config, mem_type)
                self._memory_instances.append(shared_memory)
                logger.info(f'Loaded memory: {mem_type}')
            except Exception as e:
                logger.error(f'Failed to load memory [{mem_type}]: {e}')

        self._mark_initialized()

    async def cleanup(self) -> None:
        """Cleanup memory resources."""
        self._memory_instances.clear()
        self._mark_cleanup()

    async def condense(self, messages: List[Message]) -> List[Message]:
        """
        Condense messages using all memory instances.

        Applies each memory instance's condensation logic to the messages,
        potentially summarizing or filtering based on context limits.

        Args:
            messages: Current message history.

        Returns:
            Condensed message list.
        """
        result = messages
        for memory in self._memory_instances:
            try:
                result = await memory.run(result)
            except Exception as e:
                logger.error(f'Memory condense error: {e}')
        return result

    async def retrieve(self,
                       query: str,
                       top_k: int = 5,
                       **kwargs) -> List[Dict[str, Any]]:
        """
        Retrieve relevant memories for a query.

        Args:
            query: Query string for retrieval.
            top_k: Maximum number of results.
            **kwargs: Additional retrieval parameters.

        Returns:
            List of retrieved memory items.
        """
        results = []
        for memory in self._memory_instances:
            if hasattr(memory, 'retrieve'):
                try:
                    items = await memory.retrieve(query, top_k=top_k, **kwargs)
                    results.extend(items)
                except Exception as e:
                    logger.error(f'Memory retrieve error: {e}')
        return results[:top_k]

    async def store(self,
                    messages: List[Message],
                    user_id: Optional[str] = None,
                    agent_id: Optional[str] = None,
                    run_id: Optional[str] = None,
                    memory_type: Optional[str] = None) -> None:
        """
        Store messages in memory.

        Args:
            messages: Messages to store.
            user_id: User identifier.
            agent_id: Agent identifier.
            run_id: Run identifier.
            memory_type: Specific memory type to store in.
        """
        for memory in self._memory_instances:
            if hasattr(memory, 'add'):
                try:
                    await memory.add(
                        messages,
                        user_id=user_id,
                        agent_id=agent_id,
                        run_id=run_id,
                        memory_type=memory_type)
                except Exception as e:
                    logger.error(f'Memory store error: {e}')

    def has_memory(self) -> bool:
        """Check if any memory instances are loaded."""
        return len(self._memory_instances) > 0

    def get_memory_count(self) -> int:
        """Get number of active memory instances."""
        return len(self._memory_instances)
