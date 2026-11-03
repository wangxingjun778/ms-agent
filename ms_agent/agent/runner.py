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
from typing import Any, AsyncIterator, Dict, List, Optional

from ms_agent.agent.context import RunContext
from ms_agent.agent.schema import (
    AgentResponse,
    EventType,
    StreamEvent,
    ToolResult,
)

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
            history = await ctx.session_store.load_history(ctx.session_id)

            # 2. Recall relevant memories
            memory_entries = await ctx.memory_service.recall(
                history + ctx.messages,
            )

            # 3. Build system prompt
            system_prompt = self._build_system_prompt(ctx, memory_entries)

            # 4. Assemble messages for LLM
            llm_messages = self._assemble_messages(
                system_prompt,
                history,
                ctx.messages,
            )

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

                if not tool_calls:
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
                            'content': result.content[:500],
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

                round_count += 1

            # 6. Persist session and ingest memories
            #    Compute new messages added during this run (after history +
            #    system prompt + request messages).
            pre_run_len = 1 + len(history) + len(ctx.messages)
            new_messages = llm_messages[pre_run_len:]
            if new_messages:
                await ctx.session_store.append(ctx.session_id, new_messages)
            await ctx.memory_service.ingest(ctx.session_id, llm_messages)

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
        """Call the LLM.

        Attempts to use the existing ``ms_agent.llm`` infrastructure first;
        falls back to a direct OpenAI-compatible HTTP call via aiohttp when
        the full LLM stack is unavailable.

        Returns a dict shaped as::

            {
                "message": {
                    "role": "assistant",
                    "content": "...",
                    "tool_calls": [...]   # may be absent
                },
                "usage": {
                    "prompt_tokens": int,
                    "completion_tokens": int,
                    "total_tokens": int,
                }
            }
        """
        # --- Primary path: ms_agent.llm ---
        try:
            return await self._call_llm_native(ctx, messages, tools)
        except Exception as native_exc:  # noqa: BLE001
            logger.debug(
                'AgentRunner: native LLM call failed (%s), falling back to HTTP',
                native_exc,
            )

        # --- Fallback: direct OpenAI-compatible HTTP ---
        return await self._call_llm_http(ctx, messages, tools)

    async def _call_llm_native(
        self,
        ctx: RunContext,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Call the LLM via the existing ms_agent.llm infrastructure."""
        from ms_agent.llm.llm import LLM
        from ms_agent.llm.utils import Message as LLMMessage

        # Build an LLM instance from the config if not cached in metadata
        llm_instance = ctx.metadata.get('_llm_instance')
        if llm_instance is None:
            try:
                from omegaconf import OmegaConf

                llm_dict = dict(ctx.llm_config)
                full_cfg = OmegaConf.create({'llm': llm_dict})
                llm_instance = LLM.from_config(full_cfg)
                ctx.metadata['_llm_instance'] = llm_instance
            except Exception as exc:
                raise RuntimeError(
                    f'Failed to create LLM instance: {exc}',
                ) from exc

        # Convert dict messages to LLMMessage objects
        llm_messages: List[LLMMessage] = []
        for msg in messages:
            kwargs: Dict[str, Any] = {
                'role': msg.get('role', 'user'),
                'content': msg.get('content', ''),
            }
            if msg.get('tool_calls'):
                kwargs['tool_calls'] = [
                    {
                        'id': tc.get('id', ''),
                        'type': tc.get('type', 'function'),
                        'tool_name': tc.get('function', {}).get('name', ''),
                        'arguments': tc.get('function', {}).get(
                            'arguments',
                            '{}',
                        ),
                    }
                    for tc in msg['tool_calls']
                ]
            if msg.get('tool_call_id'):
                kwargs['tool_call_id'] = msg['tool_call_id']
            if msg.get('name'):
                kwargs['name'] = msg['name']
            llm_messages.append(LLMMessage(**kwargs))

        # Convert tool specs to the internal Tool format
        llm_tools = None
        if tools:
            llm_tools = []
            for spec in tools:
                func = spec.get('function', {})
                llm_tools.append(
                    {
                        'tool_name': func.get('name', ''),
                        'description': func.get('description', ''),
                        'parameters': func.get('parameters', {}),
                    }
                )

        # Call the LLM (synchronous generate, run in thread)
        from ms_agent.llm.utils import collect_response

        response = await asyncio.to_thread(
            lambda: collect_response(
                llm_instance.generate(llm_messages, tools=llm_tools),
            ),
        )

        # Normalize into the expected dict format
        result: Dict[str, Any] = {
            'message': {
                'role': 'assistant',
                'content': getattr(response, 'content', '') or '',
            },
            'usage': {
                'prompt_tokens': getattr(response, 'prompt_tokens', 0) or 0,
                'completion_tokens': (getattr(response, 'completion_tokens', 0) or 0),
                'total_tokens': (
                    (getattr(response, 'prompt_tokens', 0) or 0) + (getattr(response, 'completion_tokens', 0) or 0)
                ),
            },
        }
        if getattr(response, 'tool_calls', None):
            result['message']['tool_calls'] = [
                {
                    'id': tc.get('id', f'call_{i}'),
                    'type': tc.get('type', 'function'),
                    'function': {
                        'name': tc.get('tool_name', ''),
                        'arguments': tc.get('arguments', '{}'),
                    },
                }
                for i, tc in enumerate(response.tool_calls)
            ]
        return result

    async def _call_llm_http(
        self,
        ctx: RunContext,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Fallback: direct OpenAI-compatible HTTP call via aiohttp."""
        try:
            import aiohttp
        except ImportError as exc:
            raise RuntimeError(
                "aiohttp is required for the HTTP LLM fallback; install via 'pip install aiohttp'",
            ) from exc

        base_url = ctx.llm_config.get(
            'base_url',
            'https://api.openai.com/v1',
        ).rstrip('/')
        api_key = ctx.llm_config.get('api_key', '')
        model = ctx.llm_config.get('model', 'gpt-4o')

        url = f'{base_url}/chat/completions'
        headers: Dict[str, str] = {
            'Content-Type': 'application/json',
        }
        if api_key:
            headers['Authorization'] = f'Bearer {api_key}'

        # Prepare request payload — strip internal fields from messages
        clean_messages = []
        for msg in messages:
            clean: Dict[str, Any] = {'role': msg['role']}
            if msg.get('content') is not None:
                clean['content'] = msg['content']
            if msg.get('tool_calls'):
                clean['tool_calls'] = msg['tool_calls']
            if msg.get('tool_call_id'):
                clean['tool_call_id'] = msg['tool_call_id']
            if msg.get('name'):
                clean['name'] = msg['name']
            clean_messages.append(clean)

        payload: Dict[str, Any] = {
            'model': model,
            'messages': clean_messages,
        }
        if tools:
            payload['tools'] = tools

        timeout = aiohttp.ClientTimeout(
            total=float(ctx.llm_config.get('timeout', 300)),
        )

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                url,
                headers=headers,
                json=payload,
            ) as resp:
                text = await resp.text()
                if resp.status != 200:
                    raise RuntimeError(
                        f'LLM HTTP {resp.status}: {text[:500]}',
                    )
                body = json.loads(text)

        # Parse the OpenAI-format response
        choices = body.get('choices') or []
        if not choices:
            raise RuntimeError(f'LLM returned no choices: {text[:500]}')
        choice = choices[0]
        message = choice.get('message', {})

        result: Dict[str, Any] = {
            'message': {
                'role': message.get('role', 'assistant'),
                'content': message.get('content', '') or '',
            },
            'usage': body.get('usage', {}),
        }
        if message.get('tool_calls'):
            result['message']['tool_calls'] = message['tool_calls']
        return result

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

            start = time.monotonic()
            try:
                result = await ctx.tool_registry.execute(name, args)
                # Adapt from api_adapter.ToolResult to schema.ToolResult
                # if they are different types.
                if not isinstance(result, ToolResult):
                    result = ToolResult(
                        tool_name=getattr(result, 'tool_name', name),
                        success=getattr(result, 'success', True),
                        content=getattr(result, 'content', str(result)),
                        raw_data=getattr(result, 'raw_data', None),
                        error=getattr(result, 'error', None),
                        metadata=dict(
                            getattr(result, 'metadata', None) or {},
                        ),
                    )
                result.metadata['duration_ms'] = int(
                    (time.monotonic() - start) * 1000,
                )
                return result
            except KeyError:
                return ToolResult.from_error(
                    name,
                    f'Tool "{name}" is not available',
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning('Tool %s failed: %s', name, exc, exc_info=True)
                return ToolResult.from_error(name, str(exc))

        return list(await asyncio.gather(*[_run_one(tc) for tc in tool_calls]))
