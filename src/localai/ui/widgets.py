"""Custom widgets: the prompt, the slash-command menu and the thinking indicator.

Three ideas here worth stating, because they are design decisions rather than
mechanics:

**The command menu sits above the input, not below.** Terminal input lives at the
bottom of the screen, so a dropdown *below* it would fall off the edge. Rising upward
also matches how the eye already moves in this app — new things appear above the
prompt.

**Thinking is shown as a caption, not a firehose.** Dumping raw reasoning is noise.
What you actually want to know is: is it working, for how long, and roughly what
about. So the indicator shows a pulse, an elapsed timer, and the *last complete
phrase* of the reasoning as a rolling caption. When it finishes it collapses to a
single line -- the thinking folds up into the answer.

**Everything degrades to ASCII.** Icons are looked up through one table so a terminal
without good Unicode support gets a usable interface rather than a broken one.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message as TextualMessage
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static, TextArea

# --- Icons ------------------------------------------------------------------
# Two sets. The Unicode ones are all in ranges Cascadia Mono covers; the ASCII
# fallback keeps column widths identical so nothing reflows when switching.

ICONS_UNICODE: dict[str, str] = {
    "user": "▎",
    "assistant": "◆",
    "thinking": "◐",
    "tool_request": "⏵",
    "tool_ok": "✓",
    "tool_fail": "✗",
    "tool_denied": "⊘",
    "warning": "⚠",
    "injection": "☣",
    "system": "◈",
    "cwd": "▸",
    "model": "◆",
    "lock_manual": "●",
    "lock_auto": "◐",
    "lock_workspace": "◑",
    "lock_bypass": "○",
    "kill": "⏻",
    "meter_full": "█",
    "meter_empty": "░",
    "spinner": "◐◓◑◒",
    "pulse": "▁▂▃▄▅▆▇█▇▆▅▄▃▂",
    "prompt": "❯",
    "search": "⌕",
}

ICONS_ASCII: dict[str, str] = {
    "user": "|",
    "assistant": "*",
    "thinking": "~",
    "tool_request": ">",
    "tool_ok": "+",
    "tool_fail": "x",
    "tool_denied": "!",
    "warning": "!",
    "injection": "!",
    "system": "*",
    "cwd": ">",
    "model": "*",
    "lock_manual": "#",
    "lock_auto": "=",
    "lock_workspace": "-",
    "lock_bypass": "o",
    "kill": "X",
    "meter_full": "#",
    "meter_empty": ".",
    "spinner": "|/-\\",
    "pulse": ".:-=+*#%#*+=-.",
    "prompt": ">",
    "search": "/",
}


def icons(unicode_ok: bool = True) -> dict[str, str]:
    return ICONS_UNICODE if unicode_ok else ICONS_ASCII


class PromptArea(TextArea):
    """Multiline input where Enter sends and Shift+Enter inserts a newline.

    The interception has to live here rather than in an App-level ``on_key``:
    ``TextArea`` handles keys in its own ``_on_key`` and consumes Enter to insert a
    newline, so the event never bubbles to the App and an App-level handler is
    silently dead code.

    It also emits :class:`Changed` on every edit so the command menu can filter live,
    and forwards navigation keys to the menu when it is open -- otherwise Up/Down
    would move the cursor instead of the selection.
    """

    class Submitted(TextualMessage):
        def __init__(self, text: str) -> None:
            self.text = text
            super().__init__()

    class TextEdited(TextualMessage):
        """Posted after any edit.

        Deliberately NOT named ``Changed``: ``TextArea`` already defines
        ``Changed`` and posts it as ``self.Changed(self)``. A nested class of the
        same name shadows it, so TextArea's own message would be constructed
        through our ``__init__`` and arrive carrying the widget instead of text.
        """

        def __init__(self, text: str) -> None:
            self.text = text
            super().__init__()

    class NavigateMenu(TextualMessage):
        """Up/Down/Tab pressed while the command menu is open."""

        def __init__(self, direction: str) -> None:
            self.direction = direction
            super().__init__()

    #: Set by the app while the command menu is visible.
    menu_open: bool = False

    async def _on_key(self, event: events.Key) -> None:
        # Escape is listed here for the same reason as Enter: TextArea handles keys
        # in its own _on_key and consumes them, so an App-level binding never fires
        # while the prompt has focus.
        if self.menu_open and event.key in ("up", "down", "tab", "escape"):
            event.prevent_default()
            event.stop()
            self.post_message(self.NavigateMenu(event.key))
            return

        if event.key == "enter":
            event.prevent_default()
            event.stop()
            self.post_message(self.Submitted(self.text))
            return

        await super()._on_key(event)
        # Posted after the key is applied so the menu filters on the new text.
        self.post_message(self.TextEdited(self.text))


#: Rows shown at once. Six is enough to choose from without covering the
#: conversation; the rest are reachable by typing another character.
VISIBLE_ROWS = 6


@dataclass(frozen=True, slots=True)
class MenuEntry:
    """One row in the command menu."""

    name: str
    summary: str
    usage: str = ""


class CommandMenu(Widget):
    """Spotlight-style command suggestions, rising above the prompt.

    Hidden unless the input starts with ``/``. Filters as you type, wraps at both
    ends when navigating, and reports the highlighted entry so the app can complete
    it on Tab or Enter.
    """

    # No DEFAULT_CSS: layout for this widget lives in ui/theme.tcss so it can be
    # reasoned about alongside everything else. An earlier DEFAULT_CSS here set
    # `dock: bottom; layer: menu`, which quietly removed the widget from its
    # container's vertical flow and let it render on top of the prompt.

    entries: reactive[tuple[MenuEntry, ...]] = reactive(())
    index: reactive[int] = reactive(0)

    #: Rows this widget currently needs. The app sums these to size the footer,
    #: because an auto-height container does not recompute when a child's display
    #: toggles -- the widget would simply overflow upward across the input box.
    wanted_height: int = 0

    def __init__(self, *, unicode_ok: bool = True, **kwargs) -> None:
        super().__init__(**kwargs)
        self._icons = icons(unicode_ok)

    @property
    def is_open(self) -> bool:
        return bool(self.entries) and self.has_class("visible")

    @property
    def selected(self) -> MenuEntry | None:
        if not self.entries:
            return None
        return self.entries[self.index % len(self.entries)]

    def show(self, entries: list[MenuEntry]) -> None:
        """Display the given entries, resetting the selection to the top."""
        if not entries:
            self.hide()
            return
        self.entries = tuple(entries)
        self.index = 0
        self.add_class("visible")
        self._resize()

    def hide(self) -> None:
        self.entries = ()
        self.remove_class("visible")
        self.wanted_height = 0
        self.styles.height = 0

    def move(self, direction: str) -> None:
        """Move the selection, wrapping at both ends."""
        if not self.entries:
            return
        step = -1 if direction == "up" else 1
        self.index = (self.index + step) % len(self.entries)
        self._resize()

    def _resize(self) -> None:
        """Set an explicit height and relayout.

        Toggling ``display`` alone does not invalidate an auto-height ancestor, so
        the menu would overflow across the input box rather than pushing the
        conversation up. An explicit height forces the parent to recompute.
        """
        rows = min(len(self.entries), VISIBLE_ROWS)
        extra = 1 if len(self.entries) > VISIBLE_ROWS else 0
        self.wanted_height = rows + extra + 1  # + the hint line
        self.styles.height = self.wanted_height

    def render(self) -> str:
        if not self.entries:
            return ""
        marker = self._icons["prompt"]
        lines: list[str] = []
        width = max(len(e.name) for e in self.entries) + 1
        # Scroll the window so the highlighted row is always visible.
        active = self.index % len(self.entries)
        first = max(0, min(active - VISIBLE_ROWS + 1, len(self.entries) - VISIBLE_ROWS))
        first = max(first, 0)
        window = self.entries[first : first + VISIBLE_ROWS]
        for offset, entry in enumerate(window):
            position = first + offset
            selected = position == active
            prefix = f"[b]{marker}[/b] " if selected else "  "
            name = f"[b]/{entry.name}[/b]" if selected else f"[dim]/{entry.name}[/dim]"
            pad = " " * (width - len(entry.name))
            summary = entry.summary if selected else f"[dim]{entry.summary}[/dim]"
            lines.append(f"{prefix}{name}{pad} {summary}")
        if len(self.entries) > VISIBLE_ROWS:
            lines.append(f"  [dim]+{len(self.entries) - VISIBLE_ROWS} more — keep typing[/dim]")
        lines.append("  [dim]↑↓ choose · Tab complete · Enter run · Esc dismiss[/dim]")
        return "\n".join(lines)


class ThinkingIndicator(Widget):
    """Live reasoning display: a pulse, an elapsed timer and a rolling caption.

    Answers the three questions you actually have while waiting: is it working, how
    long has it been, and what is it thinking about. The caption shows the most recent
    complete phrase of the reasoning rather than the raw stream, because a firehose of
    partial tokens tells you nothing.

    On completion it collapses to one line -- ``▸ thought for 4.2s`` -- which stays in
    the transcript as a record without taking space.
    """

    # No DEFAULT_CSS: layout for this widget lives in ui/theme.tcss so it can be
    # reasoned about alongside everything else. An earlier DEFAULT_CSS here set
    # `dock: bottom; layer: menu`, which quietly removed the widget from its
    # container's vertical flow and let it render on top of the prompt.

    elapsed: reactive[float] = reactive(0.0)
    caption: reactive[str] = reactive("")
    frame: reactive[int] = reactive(0)

    #: See CommandMenu.wanted_height.
    wanted_height: int = 0

    def __init__(self, *, unicode_ok: bool = True, **kwargs) -> None:
        super().__init__(**kwargs)
        self._icons = icons(unicode_ok)
        self._started: float | None = None
        self._buffer: list[str] = []
        self._label = "thinking"

    def start(self, label: str = "thinking") -> None:
        self._started = time.monotonic()
        self._label = label
        self._buffer = []
        self.caption = ""
        self.elapsed = 0.0
        self.add_class("active")
        self._resize()
        # 10 Hz: fast enough that the pulse reads as motion, slow enough to be free.
        # Guarded because start() is also called from unit tests where the widget is
        # not mounted in a running app and there is no event loop to schedule on.
        if self.is_mounted:
            self.set_interval(0.1, self._tick, name="thinking-tick")

    def _tick(self) -> None:
        if self._started is None:
            return
        self.elapsed = time.monotonic() - self._started
        self.frame += 1
        if self.is_mounted:
            self._resize()

    def feed(self, delta: str) -> None:
        """Add reasoning text; the caption updates at phrase boundaries.

        Waiting for a sentence boundary is what makes this readable — updating on
        every token produces a strobing half-word that is harder to read than nothing.
        """
        self._buffer.append(delta)
        text = "".join(self._buffer)
        for terminator in (". ", "? ", "! ", "\n"):
            if terminator in text[-120:]:
                # Split before normalising whitespace. Stripping the newline
                # terminator produces an empty string, which ``rsplit`` rejects.
                phrase = text.rsplit(terminator, 1)[0]
                phrase = " ".join(phrase.split())
                tail = phrase.split(". ")[-1].strip()
                if len(tail) > 3:
                    self.caption = tail[:110]
                break
        else:
            # No boundary yet: show the tail so something moves.
            if len(text) > 12:
                self.caption = " ".join(text.replace("\n", " ").split())[-110:]
        self.refresh()

    def stop(self) -> float:
        """Hide the indicator and return the elapsed seconds.

        Computed from the start time rather than read from :attr:`elapsed`, because
        that attribute only advances when the interval timer fires. A turn that
        finishes between ticks -- or one running headlessly with no timer at all --
        would otherwise report zero seconds for work that genuinely took time.
        """
        duration = (time.monotonic() - self._started) if self._started is not None else 0.0
        self._started = None
        self.remove_class("active")
        self.wanted_height = 0
        self.styles.height = 0
        if self.is_mounted:
            for timer in list(self._timers):
                timer.stop()
            self.refresh()
        return duration

    def _resize(self) -> None:
        """Explicit height so the footer grows; see CommandMenu._resize."""
        self.wanted_height = 2 if self.caption else 1
        self.styles.height = self.wanted_height

    def render(self) -> str:
        if self._started is None:
            return ""
        pulse = self._icons["pulse"]
        # A travelling wave rather than a spinner: it reads as ongoing effort.
        window = 14
        offset = self.frame % len(pulse)
        wave = "".join(pulse[(offset + i) % len(pulse)] for i in range(window))
        header = (
            f"[b]{self._icons['thinking']} {self._label}[/b]  "
            f"[dim]{wave}[/dim]  [b]{self.elapsed:.1f}s[/b]"
        )
        if not self.caption:
            return header
        return f"{header}\n  [dim italic]{self.caption}[/dim italic]"


class StatusBar(Static):
    """Bottom bar. Presentation only; the app supplies the text."""


class TopBar(Static):
    """Model, permission mode, working directory and context meter."""


class InputFrame(Vertical):
    """Wrapper that lets the prompt border take the active model's accent colour."""

    def compose(self) -> ComposeResult:
        return iter(())
