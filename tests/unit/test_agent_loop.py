"""Agent loop: guard rails, cancellation, tool round-trips and the fallback path."""

from __future__ import annotations

import asyncio

import pytest

from localai.agent import fallback
from localai.agent.loop import AgentLoop, EventKind
from localai.domain.messages import Message, Role, TokenSource
from localai.providers.mock import MockProvider, ScriptedResponse
from localai.tools.base import ToolContext
from localai.tools.runner import ToolRunner


async def collect(loop: AgentLoop, **kwargs) -> list:
    return [event async for event in loop.run_turn(**kwargs)]


def user(text: str) -> list[Message]:
    return [Message(role=Role.USER, content=text)]


# --- basic flow -------------------------------------------------------------


async def test_plain_answer_ends_the_turn(
    provider: MockProvider, runner: ToolRunner, context: ToolContext
) -> None:
    provider.queue_text("The answer is 42.")
    loop = AgentLoop(provider, runner)
    events = await collect(
        loop, model="mock-tools:8b", messages=user("what?"), tools=[], context=context
    )

    kinds = [e.kind for e in events]
    assert EventKind.TURN_START in kinds
    assert EventKind.TURN_COMPLETE in kinds
    assert loop.last_result.iterations == 1
    assert loop.last_result.tool_calls == 0
    assert loop.last_result.final_text == "The answer is 42."


async def test_content_is_streamed_in_pieces(
    provider: MockProvider, runner: ToolRunner, context: ToolContext
) -> None:
    """Streaming must actually stream, not arrive as one lump."""
    provider.queue(ScriptedResponse(text="a" * 100, chunk_size=10))
    loop = AgentLoop(provider, runner)
    events = await collect(
        loop, model="mock-tools:8b", messages=user("hi"), tools=[], context=context
    )
    deltas = [e for e in events if e.kind is EventKind.CONTENT_DELTA]
    assert len(deltas) == 10
    assert "".join(e.text for e in deltas) == "a" * 100


async def test_tool_call_round_trip(
    provider: MockProvider, runner: ToolRunner, context: ToolContext, registry
) -> None:
    provider.queue_tool_call("read_file", {"path": "readme.txt"})
    provider.queue_text("The budget is 48000 GBP.")

    loop = AgentLoop(provider, runner)
    events = await collect(
        loop,
        model="mock-tools:8b",
        messages=user("read it"),
        tools=[registry.get("read_file")],
        context=context,
    )

    requested = [e for e in events if e.kind is EventKind.TOOL_REQUESTED]
    results = [e for e in events if e.kind is EventKind.TOOL_RESULT]
    assert len(requested) == 1
    assert len(results) == 1
    assert results[0].tool_result.ok
    assert "Project Aurora" in results[0].tool_result.content
    assert loop.last_result.iterations == 2
    assert loop.last_result.tool_calls == 1

    # The tool result must reach the model on the second call.
    second_call = provider.calls[1]["messages"]
    assert any(m["role"] == "tool" for m in second_call)


async def test_tool_schemas_are_sent_for_capable_models(
    provider: MockProvider, runner: ToolRunner, context: ToolContext, registry
) -> None:
    provider.queue_text("done")
    loop = AgentLoop(provider, runner)
    await collect(
        loop,
        model="mock-tools:8b",
        messages=user("hi"),
        tools=[registry.get("read_file")],
        context=context,
        model_info=await provider.show_model("mock-tools:8b"),
    )
    assert provider.calls[0]["tools"], "tool schemas were not sent"


# --- guard rails ------------------------------------------------------------


async def test_iteration_cap_stops_a_runaway_loop(
    provider: MockProvider, runner: ToolRunner, context: ToolContext, registry
) -> None:
    """A model that keeps calling tools forever is stopped, with an explanation."""
    context.config.agent.max_tool_iterations = 3
    context.config.agent.max_identical_calls = 99  # isolate the iteration guard
    for index in range(10):
        provider.queue_tool_call("list_directory", {"path": ".", "limit": 10 + index})

    loop = AgentLoop(provider, runner)
    events = await collect(
        loop,
        model="mock-tools:8b",
        messages=user("go"),
        tools=[registry.get("list_directory")],
        context=context,
    )

    guards = [e for e in events if e.kind is EventKind.GUARD_TRIPPED]
    assert len(guards) == 1
    assert guards[0].detail["guard"] == "max_tool_iterations"
    assert loop.last_result.iterations == 3


async def test_repeated_identical_call_is_caught(
    provider: MockProvider, runner: ToolRunner, context: ToolContext, registry
) -> None:
    """The common small-model failure: re-reading the same file forever."""
    context.config.agent.max_identical_calls = 2
    for _ in range(6):
        provider.queue_tool_call("read_file", {"path": "readme.txt"})

    loop = AgentLoop(provider, runner)
    events = await collect(
        loop,
        model="mock-tools:8b",
        messages=user("go"),
        tools=[registry.get("read_file")],
        context=context,
    )

    guards = [e for e in events if e.kind is EventKind.GUARD_TRIPPED]
    assert guards and guards[0].detail["guard"] == "max_identical_calls"
    assert loop.last_result.tool_calls <= 3


async def test_different_arguments_do_not_trip_the_repetition_guard(
    provider: MockProvider, runner: ToolRunner, context: ToolContext, registry
) -> None:
    context.config.agent.max_identical_calls = 2
    for name in ("readme.txt", "docs/notes.md", "src/main.py"):
        provider.queue_tool_call("read_file", {"path": name})
    provider.queue_text("read them all")

    loop = AgentLoop(provider, runner)
    events = await collect(
        loop,
        model="mock-tools:8b",
        messages=user("go"),
        tools=[registry.get("read_file")],
        context=context,
    )
    assert not [e for e in events if e.kind is EventKind.GUARD_TRIPPED]
    assert loop.last_result.tool_calls == 3


async def test_turn_time_budget_is_enforced(
    provider: MockProvider, runner: ToolRunner, context: ToolContext, registry
) -> None:
    context.config.agent.max_turn_seconds = 0.001
    context.config.agent.max_identical_calls = 99
    for index in range(5):
        provider.queue_tool_call("list_directory", {"path": ".", "limit": 10 + index})

    loop = AgentLoop(provider, runner)
    await asyncio.sleep(0.01)
    events = await collect(
        loop,
        model="mock-tools:8b",
        messages=user("go"),
        tools=[registry.get("list_directory")],
        context=context,
    )
    guards = [e for e in events if e.kind is EventKind.GUARD_TRIPPED]
    assert guards and guards[0].detail["guard"] == "max_turn_seconds"


# --- cancellation -----------------------------------------------------------


async def test_cancellation_before_generation(
    provider: MockProvider, runner: ToolRunner, context: ToolContext
) -> None:
    cancel = asyncio.Event()
    cancel.set()
    context.cancel = cancel
    provider.queue_text("should not be produced")

    loop = AgentLoop(provider, runner)
    events = await collect(
        loop, model="mock-tools:8b", messages=user("hi"), tools=[], context=context
    )
    assert any(e.kind is EventKind.CANCELLED for e in events)
    assert loop.last_result.cancelled


async def test_cancellation_preserves_partial_output(
    provider: MockProvider, runner: ToolRunner, context: ToolContext
) -> None:
    """Cancelling means "stop", not "discard what you already said"."""
    cancel = asyncio.Event()
    context.cancel = cancel
    provider.queue(ScriptedResponse(text="x" * 200, chunk_size=10))

    loop = AgentLoop(provider, runner)
    events = []
    async for event in loop.run_turn(
        model="mock-tools:8b", messages=user("hi"), tools=[], context=context
    ):
        events.append(event)
        if event.kind is EventKind.CONTENT_DELTA and len(events) > 4:
            cancel.set()

    assert loop.last_result.cancelled
    partial = [m for m in loop.last_result.messages if m.role is Role.ASSISTANT]
    assert partial and partial[0].content
    assert partial[0].metadata.get("cancelled") is True


# --- provider failure -------------------------------------------------------


async def test_provider_error_is_reported_not_raised(
    runner: ToolRunner, context: ToolContext
) -> None:
    """A dead Ollama must not crash the session."""
    provider = MockProvider()
    loop = AgentLoop(provider, runner)
    events = await collect(
        loop, model="does-not-exist:1b", messages=user("hi"), tools=[], context=context
    )
    errors = [e for e in events if e.kind is EventKind.ERROR]
    assert errors
    assert "not installed" in errors[0].text
    assert errors[0].detail["remediation"]


# --- usage accounting -------------------------------------------------------


async def test_usage_is_recorded_and_marked_reported(
    provider: MockProvider, runner: ToolRunner, context: ToolContext
) -> None:
    provider.queue(ScriptedResponse(text="hello", prompt_tokens=100, completion_tokens=20))
    loop = AgentLoop(provider, runner)
    await collect(loop, model="mock-tools:8b", messages=user("hi"), tools=[], context=context)

    usage = loop.last_result.combined_usage()
    assert usage.prompt_tokens == 100
    assert usage.completion_tokens == 20
    assert usage.token_source is TokenSource.REPORTED


async def test_combined_usage_degrades_to_the_weakest_source(
    provider: MockProvider, runner: ToolRunner, context: ToolContext, registry
) -> None:
    """One unreported generation makes the whole turn's total inexact.

    Reporting a mixed total as though it were exact would be the dishonest option.
    """
    # Two generations: the first reports counts, the second does not.
    provider.queue_tool_call("read_file", {"path": "readme.txt"})  # report_tokens=True
    provider.queue(ScriptedResponse(text="done", report_tokens=False))

    loop = AgentLoop(provider, runner)
    await collect(
        loop,
        model="mock-tools:8b",
        messages=user("go"),
        tools=[registry.get("read_file")],
        context=context,
    )
    assert loop.last_result.combined_usage().token_source is TokenSource.UNKNOWN


async def test_thinking_tokens_are_always_estimated(
    provider: MockProvider, runner: ToolRunner, context: ToolContext
) -> None:
    provider.queue_text("answer", thinking="let me consider this carefully")
    loop = AgentLoop(provider, runner)
    await collect(loop, model="mock-tools:8b", messages=user("hi"), tools=[], context=context)

    usage = loop.last_result.combined_usage()
    assert usage.thinking_tokens > 0
    assert usage.thinking_token_source is TokenSource.ESTIMATED


# --- structured fallback ----------------------------------------------------


class TestFallbackParsing:
    """The parser is forgiving about framing and strict about content."""

    def test_parses_a_fenced_tool_call(self) -> None:
        calls, remaining = fallback.parse_tool_calls(
            'Here you go:\n```tool_call\n{"tool": "read_file", "arguments": {"path": "a.txt"}}\n```'
        )
        assert len(calls) == 1
        assert calls[0].name == "read_file"
        assert calls[0].arguments == {"path": "a.txt"}
        assert "Here you go" in remaining

    @pytest.mark.parametrize("fence", ["tool_call", "json", "tool", ""])
    def test_accepts_any_fence_label(self, fence: str) -> None:
        calls, _ = fallback.parse_tool_calls(f'```{fence}\n{{"tool": "x", "arguments": {{}}}}\n```')
        assert len(calls) == 1

    def test_accepts_an_unfenced_object(self) -> None:
        calls, _ = fallback.parse_tool_calls('{"tool": "read_file", "arguments": {"path": "a"}}')
        assert len(calls) == 1

    @pytest.mark.parametrize("alias", ["arguments", "args", "parameters"])
    def test_accepts_argument_aliases(self, alias: str) -> None:
        calls, _ = fallback.parse_tool_calls(
            f'```tool_call\n{{"tool": "x", "{alias}": {{"k": 1}}}}\n```'
        )
        assert calls[0].arguments == {"k": 1}

    def test_repairs_a_trailing_comma(self) -> None:
        calls, _ = fallback.parse_tool_calls(
            '```tool_call\n{"tool": "x", "arguments": {"a": 1,},}\n```'
        )
        assert len(calls) == 1

    def test_arguments_as_a_json_string_are_parsed(self) -> None:
        calls, _ = fallback.parse_tool_calls(
            '```tool_call\n{"tool": "x", "arguments": "{\\"p\\": 1}"}\n```'
        )
        assert calls[0].arguments == {"p": 1}

    @pytest.mark.parametrize(
        "text",
        [
            "no tool call here at all",
            "```python\nprint('hi')\n```",
            '```tool_call\n{"missing": "tool key"}\n```',
            "```tool_call\nnot json\n```",
            '```tool_call\n{"tool": "", "arguments": {}}\n```',
            '```tool_call\n{"tool": "x", "arguments": [1,2]}\n```',
        ],
    )
    def test_rejects_malformed_or_absent_calls(self, text: str) -> None:
        calls, _ = fallback.parse_tool_calls(text)
        assert calls == []

    def test_prose_survives_extraction(self) -> None:
        _, remaining = fallback.parse_tool_calls(
            'I will check.\n```tool_call\n{"tool": "x", "arguments": {}}\n```\nStand by.'
        )
        assert "I will check." in remaining
        assert "Stand by." in remaining


async def test_fallback_mode_engages_for_a_model_without_tools(
    provider: MockProvider, runner: ToolRunner, context: ToolContext, registry
) -> None:
    """A model with no ``tools`` capability still gets tool use, via text protocol."""
    provider.queue_text(
        '```tool_call\n{"tool": "read_file", "arguments": {"path": "readme.txt"}}\n```'
    )
    provider.queue_text("The budget is 48000 GBP.")

    loop = AgentLoop(provider, runner)
    events = await collect(
        loop,
        model="mock-plain:3b",
        messages=user("read it"),
        tools=[registry.get("read_file")],
        context=context,
        model_info=await provider.show_model("mock-plain:3b"),
    )

    assert events[0].detail["fallback_mode"] is True
    results = [e for e in events if e.kind is EventKind.TOOL_RESULT]
    assert len(results) == 1 and results[0].tool_result.ok

    # Instructions injected as a system message, no native schemas sent.
    assert provider.calls[0]["messages"][0]["role"] == "system"
    assert "tool_call" in provider.calls[0]["messages"][0]["content"]
    assert not provider.calls[0]["tools"]

    # The result comes back as a user turn, since there is no `tool` role.
    assert any(
        m["role"] == "user" and "tool result" in m["content"] for m in provider.calls[1]["messages"]
    )


async def test_fallback_instructions_preserve_the_user_system_prompt(
    provider: MockProvider, runner: ToolRunner, context: ToolContext, registry
) -> None:
    provider.queue_text("ok")
    loop = AgentLoop(provider, runner)
    await collect(
        loop,
        model="mock-plain:3b",
        messages=[
            Message(role=Role.SYSTEM, content="You are a careful assistant."),
            Message(role=Role.USER, content="hi"),
        ],
        tools=[registry.get("read_file")],
        context=context,
        model_info=await provider.show_model("mock-plain:3b"),
    )
    system = provider.calls[0]["messages"][0]["content"]
    assert system.startswith("You are a careful assistant.")
    assert "tool_call" in system
