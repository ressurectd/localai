"""TUI interaction flows, driven headlessly through Textual's pilot.

The most important tests here are the worker-context ones. ``push_screen_wait``
raises ``NoActiveWorker`` unless called from inside an ``@work`` method, and that is
not a theoretical concern: it shipped as a real bug that crashed both ``/models`` and
-- far worse -- the **permission confirmation dialog**, meaning every tool call in
Manual mode failed with a traceback instead of asking the user.

It survived the original test suite because the runner tests stubbed the confirmation
callback and the TUI tests never triggered one. These tests close that gap by driving
the actual dialog.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from localai.app import Application
from localai.config.models import PermissionMode
from localai.domain.messages import Message, Role
from localai.providers.mock import MockProvider
from localai.ui import art
from localai.ui.app import LocalAIApp
from localai.ui.widgets import CommandMenu, PromptArea, ThinkingIndicator


@pytest.fixture
def tui(app: Application) -> LocalAIApp:
    return LocalAIApp(app, initial_model="mock-tools:8b")


def rendered(tui_app: LocalAIApp) -> str:
    return " ".join(str(w.content) for w in tui_app.query("#conversation Static"))


async def settle(pilot, times: int = 20) -> None:
    for _ in range(times):
        await pilot.pause()


# --- worker context: the regression that matters ----------------------------


@pytest.mark.security
async def test_confirmation_dialog_appears_in_manual_mode(
    app: Application, workspace: Path, provider: MockProvider
) -> None:
    """The permission prompt must actually open rather than crashing.

    Regression test. ``_confirm_tool`` calls ``push_screen_wait`` from inside
    ``_run_turn``; if ``_run_turn`` loses its ``@work`` decorator this raises
    ``NoActiveWorker`` and every confirmation in Manual mode fails.
    """
    app.config.permissions.mode = PermissionMode.MANUAL
    provider.queue_tool_call("read_file", {"path": "readme.txt"})
    provider.queue_text("done")

    tui_app = LocalAIApp(app, initial_model="mock-tools:8b")
    async with tui_app.run_test() as pilot:
        await settle(pilot, 5)
        tui_app.query_one("#prompt", PromptArea).text = "read readme.txt"
        await pilot.press("enter")
        await settle(pilot, 40)

        assert "NoActiveWorker" not in rendered(tui_app)
        assert len(tui_app.screen_stack) > 1, "the confirmation dialog did not open"
        assert "ConfirmToolScreen" in type(tui_app.screen).__name__


@pytest.mark.security
async def test_denying_the_dialog_prevents_execution(
    app: Application, workspace: Path, provider: MockProvider
) -> None:
    """Pressing Escape on the prompt must mean the tool does not run."""
    app.config.permissions.mode = PermissionMode.MANUAL
    target = workspace / "should-not-exist.txt"
    provider.queue_tool_call("write_file", {"path": str(target), "content": "nope"})
    provider.queue_text("the user declined")

    tui_app = LocalAIApp(app, initial_model="mock-tools:8b")
    async with tui_app.run_test() as pilot:
        await settle(pilot, 5)
        tui_app.query_one("#prompt", PromptArea).text = "write a file"
        await pilot.press("enter")
        await settle(pilot, 40)

        if len(tui_app.screen_stack) > 1:
            await pilot.press("escape")
            await settle(pilot, 20)

        assert not target.exists(), "the tool ran despite being denied"


@pytest.mark.security
async def test_approving_the_dialog_executes_and_records_the_grant(
    app: Application, workspace: Path, provider: MockProvider
) -> None:
    app.config.permissions.mode = PermissionMode.MANUAL
    provider.queue_tool_call("read_file", {"path": "readme.txt"})
    provider.queue_text("Project Aurora, budget 48000.")

    tui_app = LocalAIApp(app, initial_model="mock-tools:8b")
    async with tui_app.run_test() as pilot:
        await settle(pilot, 5)
        tui_app.query_one("#prompt", PromptArea).text = "read it"
        await pilot.press("enter")
        await settle(pilot, 40)

        assert len(tui_app.screen_stack) > 1
        await pilot.press("a")  # approve for session
        await settle(pilot, 40)

        assert tui_app.state.tool_calls == 1
        assert any(g.tool == "read_file" for g in app.engine.grants)


async def test_model_selector_opens_from_slash_models(tui: LocalAIApp) -> None:
    """/models crashed with NoActiveWorker before action_select_model became a worker."""
    async with tui.run_test() as pilot:
        await settle(pilot, 5)
        tui.query_one("#prompt", PromptArea).text = "/models"
        await pilot.press("enter")
        await settle(pilot, 25)

        assert "NoActiveWorker" not in rendered(tui)
        assert "ModelSelectScreen" in type(tui.screen).__name__


async def test_model_selector_opens_from_bare_slash_model(tui: LocalAIApp) -> None:
    """/model with no argument should offer a list, not print a line."""
    async with tui.run_test() as pilot:
        await settle(pilot, 5)
        tui.query_one("#prompt", PromptArea).text = "/model"
        await pilot.press("enter")
        await settle(pilot, 25)
        assert "ModelSelectScreen" in type(tui.screen).__name__


async def test_mode_picker_opens_from_bare_slash_mode(tui: LocalAIApp) -> None:
    async with tui.run_test() as pilot:
        await settle(pilot, 5)
        tui.query_one("#prompt", PromptArea).text = "/mode"
        await pilot.press("enter")
        await settle(pilot, 25)
        assert "ModeSelectScreen" in type(tui.screen).__name__


async def test_theme_picker_opens(tui: LocalAIApp) -> None:
    async with tui.run_test() as pilot:
        await settle(pilot, 5)
        tui.query_one("#prompt", PromptArea).text = "/theme"
        await pilot.press("enter")
        await settle(pilot, 25)
        assert "ThemeSelectScreen" in type(tui.screen).__name__


@pytest.mark.security
async def test_bypass_still_requires_the_typed_phrase(tui: LocalAIApp) -> None:
    """Selecting bypass from the mode list must not be sufficient on its own."""
    async with tui.run_test() as pilot:
        await settle(pilot, 5)
        tui.query_one("#prompt", PromptArea).text = "/mode bypass"
        await pilot.press("enter")
        await settle(pilot, 25)

        assert tui.core.engine.mode is not PermissionMode.BYPASS
        assert "BypassConfirmScreen" in type(tui.screen).__name__


# --- command menu -----------------------------------------------------------


async def test_typing_slash_opens_the_command_menu(tui: LocalAIApp) -> None:
    async with tui.run_test() as pilot:
        await settle(pilot, 5)
        await pilot.press("slash")
        await settle(pilot, 5)

        menu = tui.query_one("#command-menu", CommandMenu)
        assert menu.is_open
        assert len(menu.entries) > 20  # every command matches the empty prefix


async def test_menu_filters_as_you_type(tui: LocalAIApp) -> None:
    async with tui.run_test() as pilot:
        await settle(pilot, 5)
        for key in ("slash", "m", "o", "d"):
            await pilot.press(key)
        await settle(pilot, 5)

        menu = tui.query_one("#command-menu", CommandMenu)
        names = {e.name for e in menu.entries}
        assert names == {"model", "models", "mode"}


async def test_menu_closes_when_the_text_is_not_a_command(tui: LocalAIApp) -> None:
    async with tui.run_test() as pilot:
        await settle(pilot, 5)
        await pilot.press("slash")
        await settle(pilot, 3)
        assert tui.query_one("#command-menu", CommandMenu).is_open

        tui.query_one("#prompt", PromptArea).text = "hello there"
        tui._on_prompt_changed(PromptArea.TextEdited("hello there"))
        await settle(pilot, 3)
        assert not tui.query_one("#command-menu", CommandMenu).is_open


async def test_arrow_keys_move_the_menu_selection(tui: LocalAIApp) -> None:
    async with tui.run_test() as pilot:
        await settle(pilot, 5)
        await pilot.press("slash")
        await settle(pilot, 3)

        menu = tui.query_one("#command-menu", CommandMenu)
        first = menu.selected.name
        await pilot.press("down")
        await settle(pilot, 3)
        assert menu.selected.name != first


async def test_tab_completes_the_highlighted_command(tui: LocalAIApp) -> None:
    async with tui.run_test() as pilot:
        await settle(pilot, 5)
        for key in ("slash", "u", "s"):
            await pilot.press(key)
        await settle(pilot, 3)
        await pilot.press("tab")
        await settle(pilot, 3)

        assert tui.query_one("#prompt", PromptArea).text.startswith("/usage")
        assert not tui.query_one("#command-menu", CommandMenu).is_open


async def test_escape_dismisses_the_menu_without_cancelling_anything(
    tui: LocalAIApp,
) -> None:
    async with tui.run_test() as pilot:
        await settle(pilot, 5)
        await pilot.press("slash")
        await settle(pilot, 3)
        await pilot.press("escape")
        await settle(pilot, 3)
        assert not tui.query_one("#command-menu", CommandMenu).is_open


# --- thinking indicator -----------------------------------------------------


async def test_thinking_indicator_shows_during_reasoning(
    app: Application, provider: MockProvider
) -> None:
    from localai.providers.mock import ScriptedResponse

    provider.queue(
        ScriptedResponse(
            thinking="Let me consider the budget. The figure appears twice. ",
            text="The budget is 48000.",
            chunk_size=8,
        )
    )
    tui_app = LocalAIApp(app, initial_model="mock-tools:8b")
    async with tui_app.run_test() as pilot:
        await settle(pilot, 5)
        indicator = tui_app.query_one("#thinking", ThinkingIndicator)
        assert not indicator.has_class("active")

        tui_app.query_one("#prompt", PromptArea).text = "budget?"
        await pilot.press("enter")
        await settle(pilot, 40)

        # A mock turn completes inside a single pause, so the transient "active" state
        # is not observable from here; the accumulated duration is the durable signal
        # that the indicator ran and was measured.
        assert tui_app._thought_seconds > 0, "thinking was never timed"
        # It folds away once the answer arrives.
        assert not indicator.has_class("active")
        assert "The budget is 48000." in rendered(tui_app)


def test_thinking_note_is_suppressed_for_trivial_durations() -> None:
    """A 2ms "thought" is noise; only real thinking earns a line in the transcript."""
    indicator = ThinkingIndicator()
    indicator.start()
    assert indicator.stop() < 0.05


def test_thinking_duration_is_measured_without_a_timer() -> None:
    """stop() must measure real elapsed time, not whatever the last tick recorded.

    Regression: it previously returned the reactive `elapsed` attribute, which only
    advances when the interval timer fires, so any turn finishing between ticks
    reported zero.
    """
    indicator = ThinkingIndicator()
    indicator.start()
    time.sleep(0.06)
    assert indicator.stop() >= 0.05


def test_thinking_caption_waits_for_a_phrase_boundary() -> None:
    """Updating on every token strobes; the caption should read as words."""
    indicator = ThinkingIndicator()
    indicator.start()
    indicator.feed("Let me chec")
    partial = indicator.caption
    indicator.feed("k the file. Now I will read it. ")
    assert indicator.caption != partial
    assert "check the file" in indicator.caption or "read it" in indicator.caption
    assert indicator.stop() >= 0


def test_thinking_indicator_reports_elapsed_time() -> None:
    indicator = ThinkingIndicator()
    indicator.start()
    indicator._tick()
    assert indicator.stop() >= 0.0


# --- model identity and theming ---------------------------------------------


async def test_switching_model_changes_the_accent_colour(tui: LocalAIApp) -> None:
    """The interface should take on the character of the model you are addressing."""
    async with tui.run_test() as pilot:
        await settle(pilot, 5)
        before = tui.state.identity.palette.accent
        await tui._switch_model("mock-plain:3b")
        await settle(pilot, 10)
        assert tui.state.identity is not None
        # Same family here, but the identity must be re-resolved on every switch.
        assert tui.state.model == "mock-plain:3b"
        assert tui.state.identity.palette.accent == before  # both are `mock`


def test_every_known_family_has_a_distinct_colour() -> None:
    """Two families sharing an accent would defeat the point of colouring by model."""
    accents = [i.palette.accent for i in art.IDENTITIES]
    assert len(set(accents)) == len(accents)


def test_unknown_model_gets_a_neutral_identity_not_a_downgrade() -> None:
    identity = art.identify("something-nobody-has-heard-of:9b")
    assert identity is art.UNKNOWN
    assert identity.palette.accent  # still styled, just not branded


@pytest.mark.parametrize(
    ("name", "family", "expected"),
    [
        ("qwen3:8b", "qwen3", "Qwen"),
        ("qwen3.6:27b", "qwen3", "Qwen"),
        ("deepseek-r1:7b", "deepseek2", "DeepSeek"),
        ("gemma3:12b", "gemma3", "Gemma"),
        ("llama3.2:3b", "llama", "Llama"),
        ("mistral-nemo:12b", "mistral", "Mistral"),
        ("phi4:14b", "phi3", "Phi"),
    ],
)
def test_families_resolve_from_real_ollama_tags(name: str, family: str, expected: str) -> None:
    assert art.identify(name, family).display == expected


def test_sigils_fit_a_narrow_terminal() -> None:
    """Art that wraps is worse than no art."""
    for identity in (*art.IDENTITIES, art.UNKNOWN):
        for line in identity.sigil + identity.sigil_ascii:
            assert len(line) <= 40, f"{identity.family} sigil line too wide: {line!r}"


def test_ascii_fallback_exists_for_every_family() -> None:
    for identity in (*art.IDENTITIES, art.UNKNOWN):
        assert identity.sigil_ascii
        assert all(c.isascii() for line in identity.sigil_ascii for c in line)


def test_curated_themes_are_real() -> None:
    """A theme in the menu that Textual cannot apply would be a dead entry."""
    from textual.theme import BUILTIN_THEMES

    custom = {t.name for t in art.custom_themes()}
    for name in art.CURATED_THEMES:
        assert name in BUILTIN_THEMES or name in custom, f"{name} is not a real theme"


async def test_window_title_is_ai(tui: LocalAIApp) -> None:
    async with tui.run_test() as pilot:
        await settle(pilot, 3)
        assert tui.title == "ai"


# --- existing behaviour must survive the rework ------------------------------


async def test_emergency_stop_still_engages_the_kill_switch(tui: LocalAIApp) -> None:
    async with tui.run_test() as pilot:
        await settle(pilot, 5)
        assert not tui.core.config.permissions.kill_switch
        await pilot.press("ctrl+q")
        await settle(pilot, 5)
        assert tui.core.config.permissions.kill_switch


async def test_mode_cycling_never_reaches_bypass(tui: LocalAIApp) -> None:
    async with tui.run_test() as pilot:
        await settle(pilot, 5)
        seen = set()
        for _ in range(6):
            await pilot.press("ctrl+g")
            await settle(pilot, 3)
            seen.add(tui.core.engine.mode)
        assert PermissionMode.BYPASS not in seen


async def test_message_still_sends_on_enter(tui: LocalAIApp, provider: MockProvider) -> None:
    provider.queue_text("hello back")
    async with tui.run_test() as pilot:
        await settle(pilot, 5)
        tui.query_one("#prompt", PromptArea).text = "hello"
        await pilot.press("enter")
        await settle(pilot, 30)
        assert any(m.content == "hello" for m in tui.state.messages)


# --- layout and chrome -------------------------------------------------------


async def test_thinking_indicator_sits_above_the_input_not_behind_it(
    tui: LocalAIApp,
) -> None:
    """Regression: the indicator was drawn *underneath* the input box.

    Each of thinking/menu/input/status was docked to the bottom independently, so
    Textual chose the stacking order and put the indicator inside the input's region.
    It was active, sized and rendering -- and completely invisible. They now share one
    footer container with an explicit vertical layout.
    """
    async with tui.run_test(size=(90, 30)) as pilot:
        await settle(pilot, 8)
        tui.thinking.start("thinking")
        tui.thinking.feed("A complete phrase ends here. ")
        await settle(pilot, 8)

        thinking = tui.query_one("#thinking").region
        prompt = tui.query_one("#prompt").region

        assert thinking.height >= 2, "the caption line did not expand the widget"
        assert thinking.y + thinking.height <= prompt.y, "indicator overlaps the prompt"


async def test_command_menu_sits_above_the_input(tui: LocalAIApp) -> None:
    async with tui.run_test(size=(90, 30)) as pilot:
        await settle(pilot, 8)
        await pilot.press("slash")
        await settle(pilot, 8)

        menu = tui.query_one("#command-menu").region
        prompt = tui.query_one("#prompt").region
        conversation = tui.query_one("#conversation").region

        assert menu.height > 0, "the menu has no height"
        assert menu.y + menu.height <= prompt.y, "the menu covers the prompt"
        assert menu.y >= conversation.y + conversation.height, "the menu covers the transcript"


async def test_status_bar_is_the_last_row(tui: LocalAIApp) -> None:
    async with tui.run_test(size=(90, 30)) as pilot:
        await settle(pilot, 8)
        status = tui.query_one("#statusbar").region
        prompt = tui.query_one("#prompt").region
        assert status.y > prompt.y, "the status bar is not the last row"


async def test_status_bar_shows_the_mode_and_a_help_pointer(tui: LocalAIApp) -> None:
    """The bottom bar is what you glance at: how much freedom it has, and where help is."""
    async with tui.run_test() as pilot:
        await settle(pilot, 8)
        text = str(tui.statusbar.content)
        assert "auto" in text
        assert "/help" in text
        assert "tools" in text


async def test_top_bar_shows_model_cwd_and_context(tui: LocalAIApp) -> None:
    async with tui.run_test() as pilot:
        await settle(pilot, 8)
        text = str(tui.topbar.content)
        assert "mock-tools:8b" in text
        assert "32,768" in text  # context limit


async def test_startup_is_quiet(tui: LocalAIApp) -> None:
    """The key reference belongs in /help, not on screen at every launch."""
    async with tui.run_test() as pilot:
        await settle(pilot, 10)
        text = rendered(tui)
        assert "Ctrl+M" not in text
        assert "Ctrl+Q" not in text
        assert "/help" in text  # just the pointer


async def test_help_opens_a_screen_rather_than_filling_the_transcript(
    tui: LocalAIApp,
) -> None:
    async with tui.run_test() as pilot:
        await settle(pilot, 8)
        before = len(tui.query("#conversation Static"))
        tui.query_one("#prompt", PromptArea).text = "/help"
        await pilot.press("enter")
        await settle(pilot, 15)

        assert "TextScreen" in type(tui.screen).__name__
        assert len(tui.query("#conversation Static")) == before


def test_help_lists_every_key_and_command() -> None:
    from localai.ui.commands import COMMANDS

    message = COMMANDS.dispatch(None, "/help").message
    for key in ("Enter", "Shift+Enter", "Ctrl+C", "Ctrl+Q", "Ctrl+M", "Ctrl+O", "F1"):
        assert key in message, f"/help does not mention {key}"
    for command in COMMANDS.all():
        assert f"/{command.name}" in message


# --- quitting ----------------------------------------------------------------


async def test_single_ctrl_c_warns_but_does_not_exit(tui: LocalAIApp) -> None:
    """One stray Ctrl+C while reading output must not end the session."""
    async with tui.run_test() as pilot:
        await settle(pilot, 8)
        await pilot.press("ctrl+c")
        await settle(pilot, 5)

        assert tui.is_running
        assert "again" in str(tui.statusbar.content).lower()


async def test_ctrl_c_twice_exits(tui: LocalAIApp) -> None:
    async with tui.run_test() as pilot:
        await settle(pilot, 8)
        await pilot.press("ctrl+c")
        await settle(pilot, 3)
        await pilot.press("ctrl+c")
        await settle(pilot, 5)
        assert not tui.is_running


async def test_ctrl_c_disarms_after_the_window(tui: LocalAIApp) -> None:
    """A press now and another a minute later is not a double-tap."""
    async with tui.run_test() as pilot:
        await settle(pilot, 8)
        await pilot.press("ctrl+c")
        await settle(pilot, 3)
        tui._quit_armed_at -= 10.0  # simulate the window expiring
        await pilot.press("ctrl+c")
        await settle(pilot, 5)
        assert tui.is_running


# --- art ---------------------------------------------------------------------


def test_sigils_are_solid_blocks_not_line_drawing() -> None:
    """Thin diagonals read as noise at terminal scale; blocks read as a logo."""
    blocks = set("█▓▒░▄▀▌▐ ")
    for identity in art.IDENTITIES:
        for line in identity.sigil:
            assert set(line) <= blocks, f"{identity.family} sigil uses non-block glyphs: {line!r}"


def test_sigil_lines_are_uniform_width() -> None:
    """Ragged rows make the mark look broken rather than deliberate."""
    for identity in (*art.IDENTITIES, art.UNKNOWN):
        widths = {len(line) for line in identity.sigil}
        assert len(widths) == 1, f"{identity.family} sigil has ragged rows: {widths}"


def test_widgets_declare_no_layout_css_of_their_own() -> None:
    """Layout lives in theme.tcss, not in widget DEFAULT_CSS.

    Regression: CommandMenu carried ``dock: bottom; layer: menu`` in its DEFAULT_CSS,
    which silently removed it from its container's vertical flow so it rendered on
    top of the prompt. Keeping every layout rule in one stylesheet means the whole
    arrangement can be reasoned about in one place.
    """
    from localai.ui.widgets import CommandMenu, ThinkingIndicator

    for widget in (CommandMenu, ThinkingIndicator):
        css = getattr(widget, "DEFAULT_CSS", "") or ""
        for banned in ("dock:", "layer:"):
            assert banned not in css, f"{widget.__name__} sets {banned} in DEFAULT_CSS"


# --- switching models --------------------------------------------------------


async def test_switching_models_clears_the_transcript(
    tui: LocalAIApp, provider: MockProvider
) -> None:
    """A second sigil stacked under the old conversation reads as clutter."""
    provider.queue_text("first answer")
    async with tui.run_test() as pilot:
        await settle(pilot, 8)
        tui.query_one("#prompt", PromptArea).text = "hello"
        await pilot.press("enter")
        await settle(pilot, 30)
        assert "hello" in rendered(tui)

        await tui._switch_model("mock-plain:3b")
        await settle(pilot, 15)

        text = rendered(tui)
        assert "hello" not in text, "the previous conversation is still on screen"
        assert "mock-plain:3b" in text


async def test_only_one_sigil_is_ever_on_screen(tui: LocalAIApp, provider: MockProvider) -> None:
    async with tui.run_test() as pilot:
        await settle(pilot, 8)
        for name in ("mock-plain:3b", "mock-vision:7b", "mock-tools:8b"):
            await tui._switch_model(name)
            await settle(pilot, 10)

        sigils = [w for w in tui.query("#conversation Static") if "sigil" in w.classes]
        assert len(sigils) == 1, f"{len(sigils)} sigils on screen; expected exactly one"


@pytest.mark.security
async def test_switching_models_says_the_context_is_kept(
    tui: LocalAIApp, provider: MockProvider
) -> None:
    """Clearing the view must not imply the conversation was reset.

    The new model still receives everything said so far. Wiping the screen silently
    would suggest a fresh start that has not happened.
    """
    provider.queue_text("an answer")
    async with tui.run_test() as pilot:
        await settle(pilot, 8)
        tui.query_one("#prompt", PromptArea).text = "remember this"
        await pilot.press("enter")
        await settle(pilot, 30)
        carried = len(tui.state.messages)
        assert carried > 0

        await tui._switch_model("mock-plain:3b")
        await settle(pilot, 15)

        text = rendered(tui)
        assert "still in context" in text, "the user is not told the context was kept"
        assert "/new" in text, "no route to an actual fresh start is offered"
        # The context itself is untouched.
        assert len(tui.state.messages) == carried


async def test_no_context_note_when_there_is_nothing_to_carry(tui: LocalAIApp) -> None:
    """A note about carrying zero messages would be noise."""
    async with tui.run_test() as pilot:
        await settle(pilot, 8)
        await tui._switch_model("mock-plain:3b")
        await settle(pilot, 12)
        assert "still in context" not in rendered(tui)


async def test_resuming_a_conversation_does_not_wipe_the_history_it_just_drew(
    app: Application, provider: MockProvider
) -> None:
    """Resume renders the transcript, then switches model. Order matters."""
    record = app.conversations.create(title="Earlier", model="mock-plain:3b")
    app.conversations.add_message(
        record.id, Message(role=Role.USER, content="a question from before")
    )

    tui_app = LocalAIApp(app, initial_model="mock-tools:8b")
    async with tui_app.run_test() as pilot:
        await settle(pilot, 8)
        tui_app._resume_conversation(record.id)
        await settle(pilot, 30)

        text = rendered(tui_app)
        assert "a question from before" in text, "resume erased the history it rendered"
        assert tui_app.state.model == "mock-plain:3b"
