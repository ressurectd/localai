"""Model identity: sigils, palettes and family detection.

The idea: you should never have to read text to know which mind you are talking to.
Switch to DeepSeek and the interface goes abyssal blue; switch to Qwen and it turns
jade. The whole UI takes on the character of the model, so the accent colour in the
top bar, the prompt border and the tool markers all shift together.

That is not only decoration. Knowing which model is answering matters — a 27B model
with a 262k context behaves very differently from a 3B one, and the permissions you
are comfortable granting may differ too. Colour carries that faster than a name does.

Sigils are deliberately small (max 7 lines, ~34 columns) so they punctuate the
conversation rather than dominating it. They are drawn with box-drawing and block
characters that Cascadia Mono renders correctly, with an ASCII fallback for terminals
that cannot.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Palette:
    """A model family's colour identity.

    Every colour is chosen to stay legible on both dark and light terminals; the
    ``accent`` is used for borders and the model name, ``glow`` for the sigil, and
    ``dim`` for secondary text in that family's sections.
    """

    accent: str
    glow: str
    dim: str
    label: str

    def to_dict(self) -> dict[str, str]:
        return {"accent": self.accent, "glow": self.glow, "dim": self.dim, "label": self.label}


@dataclass(frozen=True, slots=True)
class ModelIdentity:
    """Everything the UI needs to present one model family with character."""

    family: str
    display: str
    tagline: str
    palette: Palette
    sigil: tuple[str, ...]
    sigil_ascii: tuple[str, ...]
    icon: str

    def render(self, *, unicode_ok: bool = True) -> str:
        return "\n".join(self.sigil if unicode_ok else self.sigil_ascii)


# --- Sigils -----------------------------------------------------------------
# Each one tries to say something true about the model rather than being generic
# decoration. Qwen ("thousand questions") gets an interrogative gate; DeepSeek gets
# the whale from its own iconography, submerged; Gemma gets a cut gem; Mistral gets
# wind; Phi gets the golden ratio it is named for.

_QWEN = (
    "  ▄▄▄▄▄▄▄▄▄▄▄▄▄▄  ",
    "  ██████████████  ",
    "  ███  ████  ███  ",
    "  ███  ████  ███  ",
    "  ██████████████  ",
    "  ▀▀▀▀▀▀▀▀▀▀▀▀▀▀  ",
)
_QWEN_ASCII = (
    "  ##############  ",
    "  ##############  ",
    "  ###  ####  ###  ",
    "  ###  ####  ###  ",
    "  ##############  ",
    "  ##############  ",
)

_DEEPSEEK = (
    "  ░▒▓████████▓▒░  ",
    "     ▄██████▄     ",
    "   ▄████████████▄ ",
    "   ▀████████████▀ ",
    "  ░▒▓████████▓▒░  ",
)
_DEEPSEEK_ASCII = (
    "  ..::########::..",
    "     ########     ",
    "   ############## ",
    "   ############## ",
    "  ..::########::..",
)

_GEMMA = (
    "      ▄██████▄     ",
    "    ▄██████████▄   ",
    "    ████████████   ",
    "     ▀████████▀    ",
    "       ▀████▀      ",
    "         ▀▀        ",
)
_GEMMA_ASCII = (
    "      ########     ",
    "    ############   ",
    "    ############   ",
    "     ##########    ",
    "       ######      ",
    "         ##        ",
)

_LLAMA = (
    "    ▄▄      ▄▄    ",
    "    ██      ██    ",
    "    ████████████  ",
    "    ███  ██  ███  ",
    "    ████████████  ",
    "     ██      ██   ",
)
_LLAMA_ASCII = (
    "    ##      ##    ",
    "    ##      ##    ",
    "    ############  ",
    "    ###  ##  ###  ",
    "    ############  ",
    "     ##      ##   ",
)

_MISTRAL = (
    "  ░▒▓████████▓▒░  ",
    " ▒▓████████████▓▒ ",
    " ▓██████████████▓ ",
    " ▒▓████████████▓▒ ",
    "  ░▒▓████████▓▒░  ",
)
_MISTRAL_ASCII = (
    "  ..::########::..",
    " .::############::",
    " :##############: ",
    " .::############::",
    "  ..::########::..",
)

_PHI = (
    "   ██████████████ ",
    "   ████  ██  ████ ",
    "   ████  ██  ████ ",
    "   ██████████████ ",
    "        ████      ",
)
_PHI_ASCII = (
    "   ############## ",
    "   ####  ##  #### ",
    "   ####  ##  #### ",
    "   ############## ",
    "        ####      ",
)

_GENERIC = (
    "   ▄▄▄▄▄▄▄▄▄▄▄▄   ",
    "   ████████████   ",
    "   ▀▀▀▀▀▀▀▀▀▀▀▀   ",
)
_GENERIC_ASCII = (
    "   ############   ",
    "   ############   ",
    "   ############   ",
)


#: Family prefix -> identity. Matched against the model's reported family, then its
#: tag. Order matters: longer prefixes are checked first so `qwen3` beats `qwen`.
IDENTITIES: tuple[ModelIdentity, ...] = (
    ModelIdentity(
        family="qwen",
        display="Qwen",
        tagline="thousand questions",
        palette=Palette(accent="#2dd4a7", glow="#7df5cf", dim="#1a7d63", label="jade"),
        sigil=_QWEN,
        sigil_ascii=_QWEN_ASCII,
        icon="◆",
    ),
    ModelIdentity(
        family="deepseek",
        display="DeepSeek",
        tagline="from the deep",
        palette=Palette(accent="#4a7dff", glow="#8fb0ff", dim="#2a4a99", label="abyss"),
        sigil=_DEEPSEEK,
        sigil_ascii=_DEEPSEEK_ASCII,
        icon="≋",
    ),
    ModelIdentity(
        family="gemma",
        display="Gemma",
        tagline="cut and faceted",
        palette=Palette(accent="#b57bff", glow="#d4b0ff", dim="#6d47a3", label="amethyst"),
        sigil=_GEMMA,
        sigil_ascii=_GEMMA_ASCII,
        icon="◈",
    ),
    ModelIdentity(
        family="llama",
        display="Llama",
        tagline="sure-footed",
        palette=Palette(accent="#ff9f43", glow="#ffc98a", dim="#a6631f", label="amber"),
        sigil=_LLAMA,
        sigil_ascii=_LLAMA_ASCII,
        icon="▲",
    ),
    ModelIdentity(
        family="mistral",
        display="Mistral",
        tagline="a wind off the coast",
        palette=Palette(accent="#4fd6e0", glow="#9beef4", dim="#2a8a91", label="seabreeze"),
        sigil=_MISTRAL,
        sigil_ascii=_MISTRAL_ASCII,
        icon="≈",
    ),
    ModelIdentity(
        family="phi",
        display="Phi",
        tagline="small and dense",
        palette=Palette(accent="#ff6b9d", glow="#ffa8c4", dim="#a63d63", label="rose"),
        sigil=_PHI,
        sigil_ascii=_PHI_ASCII,
        icon="φ",
    ),
    ModelIdentity(
        family="granite",
        display="Granite",
        tagline="quarried",
        palette=Palette(accent="#94a3b8", glow="#cbd5e1", dim="#5a6675", label="stone"),
        sigil=_GENERIC,
        sigil_ascii=_GENERIC_ASCII,
        icon="■",
    ),
    ModelIdentity(
        family="command",
        display="Command-R",
        tagline="built to retrieve",
        palette=Palette(accent="#f4c542", glow="#ffe08a", dim="#9c7d16", label="brass"),
        sigil=_GENERIC,
        sigil_ascii=_GENERIC_ASCII,
        icon="◉",
    ),
    ModelIdentity(
        family="mock",
        display="Mock",
        tagline="synthetic, for testing",
        palette=Palette(accent="#8b8b8b", glow="#c4c4c4", dim="#5a5a5a", label="grey"),
        sigil=_GENERIC,
        sigil_ascii=_GENERIC_ASCII,
        icon="◌",
    ),
)

#: Used when nothing matches. Never treated as inferior -- an unlisted model gets a
#: neutral identity, not a downgrade.
UNKNOWN = ModelIdentity(
    family="unknown",
    display="",
    tagline="",
    palette=Palette(accent="#7aa2f7", glow="#a9c4ff", dim="#4a6398", label="default"),
    sigil=_GENERIC,
    sigil_ascii=_GENERIC_ASCII,
    icon="◇",
)


def identify(model_name: str, family: str = "") -> ModelIdentity:
    """Map a model to its visual identity.

    Checks the reported family first (Ollama gives us ``qwen3``, ``gemma3``), then
    falls back to the tag itself, so ``entity12208/editorai:v3-7b`` still resolves via
    its family even though the tag says nothing useful.
    """
    haystack = f"{family} {model_name}".lower()
    # Longest family name first, so `deepseek` is not shadowed by a shorter prefix.
    for identity in sorted(IDENTITIES, key=lambda i: -len(i.family)):
        if identity.family in haystack:
            return identity
    return UNKNOWN


def banner(model_name: str, family: str = "", *, unicode_ok: bool = True) -> str:
    """Render a model's sigil with its name and tagline beneath."""
    identity = identify(model_name, family)
    lines = list(identity.sigil if unicode_ok else identity.sigil_ascii)
    title = identity.display or model_name
    if identity.tagline:
        lines.append("")
        lines.append(f"  {title} — {identity.tagline}")
    else:
        lines.append("")
        lines.append(f"  {title}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Themes
# ---------------------------------------------------------------------------
# Textual ships 21 well-made themes, so we curate rather than reinvent: hand-rolling
# a palette that is already better elsewhere is effort spent for a worse result. Two
# custom ones are registered on top for terminals that want more character.

#: Curated built-ins, with a note on what each feels like. Order is the menu order.
CURATED_THEMES: dict[str, str] = {
    "textual-dark": "the default — balanced, readable, unopinionated",
    "tokyo-night": "deep indigo with neon accents; easy at 2am",
    "dracula": "purple and pink on near-black; high energy",
    "catppuccin-mocha": "soft pastels on warm dark; gentle on the eyes",
    "nord": "cold arctic blues; calm and low-contrast",
    "gruvbox": "retro warm earth tones; very easy to read",
    "monokai": "the classic editor palette; punchy",
    "rose-pine": "muted rose and pine; understated",
    "flexoki": "ink on paper, for dark rooms",
    "solarized-dark": "the old standard; scientifically fussy about contrast",
    "synthwave": "hot magenta and cyan on deep violet — maximum funk",
    "matrix": "green phosphor on black; you know why",
    "textual-light": "light background, for bright rooms",
    "solarized-light": "warm light background; low glare",
}


def custom_themes() -> list[object]:
    """Build the two themes Textual does not ship.

    Imported lazily so ``art`` stays usable without Textual installed -- the CLI
    imports this module for ``providers scan`` colouring and must not need a UI
    framework to do it.
    """
    from textual.theme import Theme

    return [
        Theme(
            name="synthwave",
            primary="#ff2e97",
            secondary="#00e5ff",
            accent="#f9c80e",
            foreground="#f8f0ff",
            background="#1a0b2e",
            surface="#241040",
            panel="#2f1654",
            success="#00f5a0",
            warning="#ffb627",
            error="#ff2e63",
            dark=True,
        ),
        Theme(
            name="matrix",
            primary="#00ff41",
            secondary="#008f11",
            accent="#7fff9f",
            foreground="#c8ffd4",
            background="#000000",
            surface="#0a0f0a",
            panel="#0f1a0f",
            success="#00ff41",
            warning="#c8ff00",
            error="#ff3333",
            dark=True,
        ),
    ]
