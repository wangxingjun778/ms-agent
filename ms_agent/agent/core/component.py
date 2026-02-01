# Copyright (c) ModelScope Contributors. All rights reserved.
"""
Agent component module providing base class for all components.

Components are the building blocks of the agent architecture,
each providing a specific capability (LLM, tools, memory, etc.).
"""
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Type

from ms_agent.utils.logger import get_logger

if TYPE_CHECKING:
    from .context import AgentContext

logger = get_logger()


class AgentComponent(ABC):
    """
    Base class for all agent components.

    Components encapsulate specific capabilities and are managed
    by the AgentOrchestrator. Each component has a lifecycle:
    1. Construction with configuration
    2. Initialization with context
    3. Usage during pipeline execution
    4. Cleanup after execution

    Subclasses must implement:
    - name property
    - initialize() method
    - cleanup() method

    Attributes:
        _initialized: Whether the component has been initialized.
        _config: Component configuration.
    """

    def __init__(self, config: Optional[Any] = None):
        """
        Initialize component with configuration.

        Args:
            config: Component-specific configuration.
        """
        self._config = config
        self._initialized = False

    @property
    @abstractmethod
    def name(self) -> str:
        """
        Unique component name for registration.

        Returns:
            Component name string.
        """
        raise NotImplementedError()

    @property
    def is_initialized(self) -> bool:
        """Check if component has been initialized."""
        return self._initialized

    @abstractmethod
    async def initialize(self, context: 'AgentContext') -> None:
        """
        Initialize the component with execution context.

        Called by the orchestrator before pipeline execution.
        Components should acquire resources and prepare for use.

        Args:
            context: Agent execution context.

        Raises:
            ComponentInitializationError: If initialization fails.
        """
        raise NotImplementedError()

    @abstractmethod
    async def cleanup(self) -> None:
        """
        Cleanup component resources.

        Called by the orchestrator after pipeline execution.
        Components should release resources and reset state.
        """
        raise NotImplementedError()

    def _mark_initialized(self) -> None:
        """Mark component as initialized."""
        self._initialized = True
        logger.debug(f'Component [{self.name}] initialized')

    def _mark_cleanup(self) -> None:
        """Mark component as cleaned up."""
        self._initialized = False
        logger.debug(f'Component [{self.name}] cleaned up')


class ComponentRegistry:
    """
    Registry for managing agent components.

    Provides component registration, retrieval, and lifecycle management.
    Supports both instance registration and factory-based creation.

    Attributes:
        _components: Registered component instances.
        _factories: Registered component factories.
    """

    def __init__(self):
        """Initialize empty registry."""
        self._components: Dict[str, AgentComponent] = {}
        self._factories: Dict[str, Type[AgentComponent]] = {}

    def register(self, component: AgentComponent) -> None:
        """
        Register a component instance.

        Args:
            component: Component instance to register.

        Raises:
            ValueError: If component with same name already registered.
        """
        if component.name in self._components:
            logger.warning(
                f'Component [{component.name}] already registered, replacing')
        self._components[component.name] = component
        logger.debug(f'Registered component: {component.name}')

    def register_factory(self, name: str,
                         factory: Type[AgentComponent]) -> None:
        """
        Register a component factory for lazy instantiation.

        Args:
            name: Component name.
            factory: Component class to instantiate.
        """
        self._factories[name] = factory
        logger.debug(f'Registered component factory: {name}')

    def get(self, name: str) -> Optional[AgentComponent]:
        """
        Get a registered component by name.

        Args:
            name: Component name.

        Returns:
            Component instance or None if not found.
        """
        return self._components.get(name)

    def get_or_create(
            self,
            name: str,
            config: Optional[Any] = None) -> Optional[AgentComponent]:
        """
        Get existing component or create from factory.

        Args:
            name: Component name.
            config: Configuration for factory creation.

        Returns:
            Component instance or None if not found/creatable.
        """
        if name in self._components:
            return self._components[name]

        if name in self._factories:
            component = self._factories[name](config)
            self._components[name] = component
            return component

        return None

    def has(self, name: str) -> bool:
        """Check if component is registered."""
        return name in self._components or name in self._factories

    def get_all(self) -> List[AgentComponent]:
        """Get all registered component instances."""
        return list(self._components.values())

    def get_names(self) -> List[str]:
        """Get all registered component names."""
        return list(self._components.keys())

    async def initialize_all(self, context: 'AgentContext') -> None:
        """
        Initialize all registered components.

        Args:
            context: Agent execution context.
        """
        for component in self._components.values():
            if not component.is_initialized:
                await component.initialize(context)

    async def cleanup_all(self) -> None:
        """Cleanup all registered components."""
        for component in self._components.values():
            if component.is_initialized:
                try:
                    await component.cleanup()
                except Exception as e:
                    logger.warning(
                        f'Error cleaning up component [{component.name}]: {e}')

    def clear(self) -> None:
        """Clear all registered components."""
        self._components.clear()
        logger.debug('Component registry cleared')
