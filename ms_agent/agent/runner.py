"""Stateless agent executor — the lightweight replacement for LLMAgent's run loop.

:class:`AgentRunner` implements the function-calling agent loop without any
mutable instance state.  Every piece of per-request context flows through a
:class:`~ms_agent.agent.context.RunContext`; the runner simply orchestrates
the LLM ↔ tool cycle and yields :class:`~ms_agent.agent.schema.StreamEvent`
objects for real-time output.

Two LLM call paths are supported:

1. **Primary** — reuse the existing ``ms_agent.llm`` infrastructure (the
   ``LLM`` base class and its provider-specific subclasses) when it is
   available.
2. **Fallback** — direct OpenAI-compatible HTTP call via ``aiohttp`` so the
   runner works even without the full LLM stack.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict
from typing import Any, AsyncIterator, Dict, List, Optional

from ms_agent.agent.context import RunContext
from ms_agent.agent.control import InteractionTimeout
from ms_agent.agent.schema import (
    AgentResponse,
    EventType,
    StreamEvent,
    ToolResult,
)
from ms_agent.session.locking import PersistenceError
from ms_agent.tools.schema import ExternalTaskUnresolved, ToolInvocationContext

logger = logging.getLogger(__name__)

__all__ = ['AgentRunner']


class AgentRunner:
    """Stateless agent executor.  No instance state across requests.

    The runner implements the canonical function-calling loop:

    1. Load session history.
    2. Recall relevant memories.
    3. Build the system prompt (base + skills + memories).
    4. Assemble LLM messages.
    5. Function-calling loop (up to ``ctx.max_rounds``):
       a. Call LLM with messages + tool specs.
       b. If no tool_calls → yield final response and break.
       c. Execute tool calls in parallel.
       d. Append tool results to messages.
       e. Continue.
    6. Persist session and ingest memories.
    """

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def run(self, ctx: RunContext) -> AsyncIterator[StreamEvent]:
        """Execute the agent loop, yielding StreamEvents for real-time output.

        Args:
            ctx: The fully assembled run context.

        Yields:
            :class:`StreamEvent` instances in sequence order.
        """
        seq = 0

        yield StreamEvent(
            event_type=EventType.STREAM_START,
            data={'session_id': ctx.session_id},
            seq=seq,
        )
        seq += 1

        try:
            # 1. Load session history
            history = self._history_window(
                await ctx.session_store.load_history(ctx.session_id), ctx.config.get('history', {})
            )

            # 2. Recall relevant memories
            memory_entries = (
                await ctx.memory_service.recall(
                    history + ctx.messages,
                    top_k=ctx.config.get('memory_top_k', 5),
                    exclude_messages=history + ctx.messages,
                )
                if ctx.memory_enabled
                else []
            )

            # 3. Build system prompt
            system_prompt = self._build_system_prompt(ctx, memory_entries)

            # 4. Assemble messages for LLM
            llm_messages = self._assemble_messages(
                system_prompt,
                history,
                ctx.messages,
            )

            if ctx.record is not None:
                await ctx.record(
                    'execution_context',
                    {
                        'messages': llm_messages,
                        'history_count': len(history),
                        'memories': memory_entries,
                        'skills': ctx.skill_service.get_active_skills() if ctx.skill_service else [],
                    },
                )
            await ctx.session_store.append(ctx.session_id, ctx.messages)
            persisted = len(llm_messages)
            completed = False

            # 5. Function-calling loop
            tool_specs = ctx.tool_registry.get_tool_specs()
            usage_stats: Dict[str, int] = {
                'prompt_tokens': 0,
                'completion_tokens': 0,
                'total_tokens': 0,
            }
            tool_call_records: List[Dict[str, Any]] = []
            round_count = 0

            while round_count < ctx.max_rounds:
                if ctx.control is not None:
                    await ctx.control.boundary(llm_messages)
                if ctx.record is not None:
                    await ctx.record('llm_started', {'round': round_count})
                # Call LLM
                llm_response = await self._call_llm(
                    ctx,
                    llm_messages,
                    tool_specs,
                )

                # Accumulate usage
                resp_usage = llm_response.get('usage')
                if isinstance(resp_usage, dict):
                    for key in usage_stats:
                        usage_stats[key] += int(resp_usage.get(key, 0) or 0)

                # Extract assistant message content
                assistant_msg = llm_response.get('message', {})
                content = assistant_msg.get('content', '') or ''
                tool_calls = assistant_msg.get('tool_calls') or []

                if content:
                    yield StreamEvent(
                        event_type=EventType.LLM_DELTA,
                        data={'content': content},
                        seq=seq,
                    )
                    seq += 1

                if not tool_calls and ctx.control is not None and ctx.control.interventions:
                    llm_messages.append({'role': 'assistant', 'content': content})
                    await ctx.control.boundary(llm_messages)
                    await ctx.session_store.append(ctx.session_id, llm_messages[persisted:])
                    persisted = len(llm_messages)
                    round_count += 1
                    continue

                if not tool_calls:
                    completed = True
                    # No tool calls — final response
                    yield StreamEvent(
                        event_type=EventType.LLM_COMPLETE,
                        data={'content': content, 'round': round_count},
                        seq=seq,
                    )
                    seq += 1

                    # Add assistant message to history
                    llm_messages.append(
                        {
                            'role': 'assistant',
                            'content': content,
                        }
                    )
                    break

                # Emit tool call events
                for tc in tool_calls:
                    func = tc.get('function', {})
                    yield StreamEvent(
                        event_type=EventType.LLM_TOOL_CALL,
                        data={
                            'tool_name': func.get('name', ''),
                            'arguments': func.get('arguments', '{}'),
                        },
                        seq=seq,
                    )
                    seq += 1

                # Add assistant message with tool_calls to message list
                llm_messages.append(
                    {
                        'role': 'assistant',
                        'content': content,
                        'tool_calls': tool_calls,
                    }
                )

                # Execute tools in parallel
                results = await self._execute_tools(ctx, tool_calls)

                # Emit results and append tool messages
                for tc, result in zip(tool_calls, results):
                    yield StreamEvent(
                        event_type=EventType.TOOL_RESULT,
                        data={
                            'tool_name': result.tool_name,
                            'success': result.success,
                            'content': result.content,
                            'record': asdict(result)
                            | {
                                'result': asdict(result),
                                'arguments': json.loads(tc['function']['arguments'])
                                if isinstance(tc['function']['arguments'], str)
                                else tc['function']['arguments'],
                            },
                        },
                        seq=seq,
                    )
                    seq += 1

                    func = tc.get('function', {})
                    tool_call_records.append(
                        {
                            'tool_name': result.tool_name,
                            'arguments': func.get('arguments', {}),
                            'result': result,
                            'duration_ms': result.metadata.get('duration_ms', 0),
                        }
                    )

                    # Add tool result message
                    llm_messages.append(
                        {
                            'role': 'tool',
                            'tool_call_id': tc.get('id', ''),
                            'content': result.content,
                        }
                    )

                await ctx.session_store.append(ctx.session_id, llm_messages[persisted:])
                persisted = len(llm_messages)
                round_count += 1

            if not completed:
                raise RuntimeError('MAX_ROUNDS_EXCEEDED')

            # 6. Persist session and ingest memories
            #    Compute new messages added during this run (after history +
            #    system prompt + request messages).
            new_messages = llm_messages[persisted:]
            if new_messages:
                await ctx.session_store.append(ctx.session_id, new_messages)
            if ctx.memory_enabled:
                await ctx.memory_service.ingest(ctx.session_id, llm_messages[1 + len(history) :])

            # Emit stream end
            yield StreamEvent(
                event_type=EventType.STREAM_END,
                data={
                    'usage': usage_stats,
                    'rounds': round_count,
                    'tool_calls_count': len(tool_call_records),
                },
                seq=seq,
            )

        except Exception as exc:
            logger.exception('AgentRunner: run failed')
            yield StreamEvent(
                event_type=EventType.ERROR,
                data={'error': str(exc), 'type': type(exc).__name__},
                seq=seq,
            )
            raise

    async def run_sync(self, ctx: RunContext) -> AgentResponse:
        """Convenience: collect all events and return an :class:`AgentResponse`."""
        events: List[StreamEvent] = []
        async for event in self.run(ctx):
            events.append(event)
        return AgentResponse.from_events(ctx.session_id, events)

    # ------------------------------------------------------------------
    # System prompt assembly
    # ------------------------------------------------------------------

    def _build_system_prompt(
        self,
        ctx: RunContext,
        memory_entries: List[Dict[str, Any]],
    ) -> str:
        """Build the complete system prompt (base + skills + memories)."""
        parts: List[str] = []

        # Base system prompt from config
        base_prompt = ctx.config.get(
            'system_prompt',
            'You are a helpful assistant.',
        )
        parts.append(base_prompt)

        # Skills section
        if ctx.skill_service is not None:
            skill_section = ctx.skill_service.build_skill_prompt_section()
            if skill_section:
                parts.append(skill_section)

        # Memory section
        if memory_entries:
            memory_section = self._format_memory_section(memory_entries)
            if memory_section:
                parts.append(memory_section)

        return '\n\n'.join(parts)

    @staticmethod
    def _assemble_messages(
        system_prompt: str,
        history: List[Dict[str, Any]],
        current_messages: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Assemble the full message list for the LLM."""
        messages: List[Dict[str, Any]] = [
            {'role': 'system', 'content': system_prompt},
        ]
        messages.extend(history)
        messages.extend(current_messages)
        return messages

    @staticmethod
    def _history_window(history: list[dict], config: dict) -> list[dict]:
        limit = config.get('max_messages', 0)
        if not limit:
            return history
        # 按用户回合整体裁剪，不拆开 assistant.tool_calls 和相应 tool 结果。
        groups = []
        for message in history:
            if message.get('role') == 'user' or not groups:
                groups.append([])
            groups[-1].append(message)
        selected, count, chars = [], 0, 0
        char_limit = config.get('max_chars', 0)
        for group in reversed(groups):
            size = sum(len(json.dumps(m, ensure_ascii=False)) for m in group)
            if count + len(group) > limit or (char_limit and chars + size > char_limit):
                break
            selected.append(group)
            count, chars = count + len(group), chars + size
        return [m for group in reversed(selected) for m in group]

    @staticmethod
    def _format_memory_section(entries: List[Dict[str, Any]]) -> str:
        """Format memory entries into a prompt section."""
        if not entries:
            return ''
        lines = ['## Relevant Memories', '']
        for entry in entries:
            content = str(entry.get('content', '')).strip()
            if not content:
                continue
            category = entry.get('category', '')
            prefix = f'[{category}] ' if category else ''
            lines.append(f'- {prefix}{content}')
        return '\n'.join(lines) if len(lines) > 2 else ''

    # ------------------------------------------------------------------
    # LLM calling
    # ------------------------------------------------------------------

    async def _call_llm(
        self,
        ctx: RunContext,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """使用生命周期所有者注入的客户端，不回退到旧模型执行链路。"""
        if ctx.llm_client is None:
            raise RuntimeError('轻量执行需要显式注入 LLM 客户端')
        return await ctx.llm_client.complete(messages, tools)

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    async def _execute_tools(
        self,
        ctx: RunContext,
        tool_calls: List[Dict[str, Any]],
    ) -> List[ToolResult]:
        """Execute tool calls in parallel via the API registry."""

        async def _run_one(tc: Dict[str, Any]) -> ToolResult:
            func = tc.get('function', {})
            name = func.get('name', '')
            raw_args = func.get('arguments', '{}')

            # Parse arguments
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args)
                except (json.JSONDecodeError, TypeError):
                    args = {}
            elif isinstance(raw_args, dict):
                args = raw_args
            else:
                args = {}

            func['arguments'] = json.dumps(args, ensure_ascii=False)
            start = time.monotonic()
            try:
                invocation_index = ctx.metadata.get('_invocation_index', 0)
                ctx.metadata['_invocation_index'] = invocation_index + 1
                invocation_id = f'{ctx.run_uuid}:tool:{invocation_index}'
                if ctx.control is not None:
                    args = await ctx.control.prepare_tool(
                        invocation_id,
                        name,
                        args,
                        ctx.tool_registry.parameters(name),
                        ctx.tool_registry.requires_approval(name),
                    )
                    if args is None:
                        return ToolResult.from_error(name, '工具调用已跳过', skipped=True)
                func['arguments'] = json.dumps(args, ensure_ascii=False)
                context = ToolInvocationContext(
                    ctx.user_id, ctx.session_id, ctx.run_uuid, invocation_id, record=ctx.record
                )
                await context.emit('tool_started', {'tool_name': name, 'arguments': args})
                result = await ctx.tool_registry.execute(name, args, context)
                result.metadata['duration_ms'] = int((time.monotonic() - start) * 1000)
                await context.emit('tool_completed', {'tool_name': name, 'arguments': args, 'result': asdict(result)})
                if ctx.control is not None and (
                    result.metadata.get('unresolved') or result.metadata.get('delivery_unknown')
                ):
                    raise ExternalTaskUnresolved('EXTERNAL_TASK_UNRESOLVED')
                return result
            except (PersistenceError, ExternalTaskUnresolved, InteractionTimeout):
                raise
            except KeyError:
                return ToolResult.from_error(
                    name,
                    f'Tool "{name}" is not available',
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning('Tool %s failed: %s', name, exc, exc_info=True)
                return ToolResult.from_error(name, str(exc))

        if ctx.control is not None:
            return [await _run_one(tc) for tc in tool_calls]
        return list(await asyncio.gather(*[_run_one(tc) for tc in tool_calls]))
