# Copyright (c) ModelScope Contributors. All rights reserved.
"""
Core module for agent architecture.

This module provides the foundational components for building modular,
extensible agents using a pipeline-based architecture.
"""
from .component import AgentComponent, ComponentRegistry
from .context import AgentContext, AgentState
from .event_bus import AgentEventBus, EventType
from .orchestrator import AgentOrchestrator
from .pipeline import Pipeline, PipelineStage

__all__ = [
    'AgentContext',
    'AgentState',
    'AgentComponent',
    'ComponentRegistry',
    'PipelineStage',
    'Pipeline',
    'AgentOrchestrator',
    'AgentEventBus',
    'EventType',
]
