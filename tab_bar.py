"""tabbykat — custom kitty tab bar with gradient spotlight + directional powerline arrows.

Driven entirely by tab_bar.toml — see that file for config documentation.
Run standalone with: python3 tab_bar.py --test
"""
from __future__ import annotations

import math
import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import NamedTuple

_STANDALONE = 'kitty' not in sys.modules and __name__ == '__main__'

if _STANDALONE:
    # Minimal shims for testing without kitty
    class Color(NamedTuple):  # type: ignore[no-redef]
        red: int = 0
        green: int = 0
        blue: int = 0

    class Screen:  # type: ignore[no-redef]
        pass

    def wcswidth(s: str) -> int:  # type: ignore[no-redef]
        # ASCII-only approximation; good enough for unit tests
        return len(s)

    def to_color(raw: str, validate: bool = False) -> Color | None:  # type: ignore[no-redef]
        raw = raw.strip()
        if not raw:
            return None
        if raw.startswith('#'):
            raw = raw[1:]
        if len(raw) == 6:
            try:
                return Color(int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16))
            except ValueError:
                return None
        return None

    def color_as_sgr(c: Color) -> str:  # type: ignore[no-redef]
        return f';2;{c.red};{c.green};{c.blue}'

    def color_from_int(val: int) -> Color:  # type: ignore[no-redef]
        return Color((val >> 16) & 0xff, (val >> 8) & 0xff, val & 0xff)

    def color_as_int(val: Color) -> int:  # type: ignore[no-redef]
        return (val.red << 16) | (val.green << 8) | val.blue

    def as_rgb(x: int) -> int:  # type: ignore[no-redef]
        return (x << 8) | 2

    def log_error(*a: object, **kw: object) -> None:  # type: ignore[no-redef]
        print(*a, file=sys.stderr, **kw)

    class ColorFormatter:  # type: ignore[no-redef]
        draw_data: object = None
        tab_data: object = None
        def __init__(self, which: str) -> None:
            self.which = which
        def __getattr__(self, name: str) -> str:
            return ''

    class Formatter:  # type: ignore[no-redef]
        reset = ''
        fg = ColorFormatter('3')
        bg = ColorFormatter('4')
        bold = ''
        nobold = ''
        italic = ''
        noitalic = ''

    class DrawData(NamedTuple):  # type: ignore[no-redef]
        active_bg: Color = Color()
        active_fg: Color = Color()
        inactive_bg: Color = Color()
        inactive_fg: Color = Color()
        default_bg: Color = Color()
        os_window_id: int = 0

    class TabBarData(NamedTuple):  # type: ignore[no-redef]
        title: str = ''
        is_active: bool = False

    class ExtraData:  # type: ignore[no-redef]
        prev_tab: object = None
        next_tab: object = None
        for_layout: bool = False

    def draw_attributed_string(title: str, screen: Screen) -> None:  # type: ignore[no-redef]
        pass
else:
    from kitty.fast_data_types import Color, Screen, wcswidth
    from kitty.rgb import color_as_sgr, color_from_int, to_color
    from kitty.tab_bar import (
        ColorFormatter,
        DrawData,
        ExtraData,
        Formatter,
        TabBarData,
        as_rgb,
        draw_attributed_string,
    )
    from kitty.utils import color_as_int, log_error

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_home = os.path.expanduser('~')
_config_path = Path(__file__).with_suffix('.toml')

RIGHT_ARROW = '\ue0b0'
LEFT_ARROW = '\ue0b2'

_SGR_SPLIT = re.compile(r'(\033\[.*?m)')
_PAD_SPLIT = re.compile(r'(%P|%%)')

# ---------------------------------------------------------------------------
# Config model
# ---------------------------------------------------------------------------


class ColorType(Enum):
    SOLID = 'solid'
    GRADIENT = 'gradient'


class Curve(Enum):
    LINEAR = 'linear'
    POW = 'pow'


@dataclass(frozen=True)
class ColorSection:
    type: ColorType = ColorType.SOLID
    active_color: Color | None = None
    inactive_color: Color | None = None
    ramp_length: int = 0
    curve: Curve = Curve.LINEAR
    exponent: float = 1.0


@dataclass(frozen=True)
class Config:
    tab_format: str = '{t}'
    pad_ideal_width: int = 24
    pad_char: str = ' '
    auto_contrast: int = 0  # 0=off, 50=WCAG AA (4.5:1), 100=overshoot (9:1)
    background: ColorSection = field(default_factory=ColorSection)
    foreground: ColorSection = field(default_factory=ColorSection)


def _parse_color(value: object) -> Color | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return to_color(value.strip())


def _parse_color_section(data: dict) -> ColorSection:
    raw_type = data.get('type', 'solid')
    try:
        ctype = ColorType(raw_type)
    except ValueError:
        ctype = ColorType.SOLID

    raw_curve = data.get('curve', 'linear')
    try:
        curve = Curve(raw_curve)
    except ValueError:
        curve = Curve.LINEAR

    ramp = data.get('ramp_length', 0)
    if not isinstance(ramp, int):
        ramp = 0
    ramp = max(0, ramp)

    exp = data.get('exponent', 1.0)
    if not isinstance(exp, (int, float)):
        exp = 1.0
    exp = max(0.0, float(exp))

    return ColorSection(
        type=ctype,
        active_color=_parse_color(data.get('active_color', '')),
        inactive_color=_parse_color(data.get('inactive_color', '')),
        ramp_length=ramp,
        curve=curve,
        exponent=exp,
    )


def _load_config() -> Config:
    try:
        with _config_path.open('rb') as fh:
            data = tomllib.load(fh)
    except (FileNotFoundError, tomllib.TOMLDecodeError):
        return Config()

    tab = data.get('tab', {})
    pad = data.get('pad', {})

    tab_format = tab.get('format', '{t}')
    if not isinstance(tab_format, str) or not tab_format:
        tab_format = '{t}'

    ideal_width = pad.get('ideal_width', 24)
    if not isinstance(ideal_width, int):
        ideal_width = 24
    ideal_width = max(0, ideal_width)

    pad_char = pad.get('char', ' ')
    if not isinstance(pad_char, str) or not pad_char:
        pad_char = ' '
    else:
        pad_char = pad_char[0]
    if wcswidth(pad_char) != 1:
        pad_char = ' '

    auto_contrast = data.get('auto_contrast', 0)
    if not isinstance(auto_contrast, (int, float)):
        auto_contrast = 0
    auto_contrast = max(0, min(100, int(auto_contrast)))

    return Config(
        tab_format=tab_format,
        pad_ideal_width=ideal_width,
        pad_char=pad_char,
        auto_contrast=auto_contrast,
        background=_parse_color_section(data.get('background', {})),
        foreground=_parse_color_section(data.get('foreground', {})),
    )


CONFIG = _load_config()

# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------


class _TokenType(Enum):
    TEXT = 'text'  # plain text + embedded SGR escapes — drawable via draw_attributed_string
    PAD = 'pad'    # %P marker


class _Token(NamedTuple):
    type: _TokenType
    value: str       # the drawable string (TEXT) or empty (PAD)
    width: int        # visible cell width (TEXT) or 0 (PAD)


def _tokenize(expanded: str) -> list[_Token]:
    """Split an expanded format string into TEXT and PAD tokens.

    The input has already been through str.format() so it contains plain text,
    SGR escape sequences, and %P / %% markers.
    """
    tokens: list[_Token] = []
    # First split on %P and %% markers
    parts = _PAD_SPLIT.split(expanded)
    for part in parts:
        if part == '%P':
            tokens.append(_Token(_TokenType.PAD, '', 0))
        elif part == '%%':
            tokens.append(_Token(_TokenType.TEXT, '%', 1))
        elif part:
            # Measure visible width: strip SGR escapes and measure the rest
            plain = _SGR_SPLIT.sub('', part)
            w = wcswidth(plain) if plain else 0
            tokens.append(_Token(_TokenType.TEXT, part, max(w, 0)))
    return tokens


def _tokens_width(tokens: list[_Token]) -> int:
    return sum(t.width for t in tokens)


def _tokens_pad_count(tokens: list[_Token]) -> int:
    return sum(1 for t in tokens if t.type == _TokenType.PAD)

# ---------------------------------------------------------------------------
# Title preparation
# ---------------------------------------------------------------------------

_fmt = Formatter()


def _fix_title(title: str) -> str:
    if title.startswith('…/'):
        candidate = _home + title[1:]
        if os.path.exists(candidate):
            return '~' + title[1:]
    return title


def _prepare_title(tab: TabBarData, index: int, draw_data: DrawData) -> tuple[str, list[_Token]]:
    """Expand the format string and tokenize it.

    Returns (display_title_for_tab_replace, tokens).
    """
    title = _fix_title(tab.title)

    # Bind ColorFormatter so {fmt.fg.tab} / {fmt.bg.tab} resolve correctly
    ColorFormatter.draw_data = draw_data
    ColorFormatter.tab_data = tab

    try:
        expanded = CONFIG.tab_format.format(n=index, t=title, fmt=_fmt)
    except Exception:
        log_error(f'tab_bar.toml: bad [tab].format, falling back to title')
        expanded = title

    tokens = _tokenize(expanded)
    return title, tokens

# ---------------------------------------------------------------------------
# Color helpers
# ---------------------------------------------------------------------------


def _c(color: Color) -> int:
    return as_rgb(color_as_int(color))


def _lerp_channel(a: int, b: int, t: float) -> int:
    return max(0, min(255, round(a + (b - a) * t)))


def _lerp_color(a: Color, b: Color, t: float) -> Color:
    return Color(
        _lerp_channel(a.red, b.red, t),
        _lerp_channel(a.green, b.green, t),
        _lerp_channel(a.blue, b.blue, t),
    )


def _srgb_luminance(c: Color) -> float:
    """Relative luminance per WCAG 2.x (sRGB linearization)."""
    def lin(v: int) -> float:
        s = v / 255.0
        return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4
    return 0.2126 * lin(c.red) + 0.7152 * lin(c.green) + 0.0722 * lin(c.blue)


def _contrast_ratio(fg: Color, bg: Color) -> float:
    """WCAG contrast ratio (always >= 1.0)."""
    l1 = _srgb_luminance(fg)
    l2 = _srgb_luminance(bg)
    if l1 < l2:
        l1, l2 = l2, l1
    return (l1 + 0.05) / (l2 + 0.05)


def _max_contrast(bg: Color) -> tuple[float, Color]:
    """Return (max_ratio, best_pole) for a given background."""
    bg_lum = _srgb_luminance(bg)
    white_cr = (1.0 + 0.05) / (bg_lum + 0.05)
    black_cr = (bg_lum + 0.05) / (0.0 + 0.05)
    if white_cr >= black_cr:
        return white_cr, Color(255, 255, 255)
    return black_cr, Color(0, 0, 0)


def _auto_contrast_fg(fg: Color, bg: Color) -> Color:
    """Adjust fg toward white or black until the configured contrast target is met.

    Strategy:
    1. If already at target, return unchanged.
    2. Compute max achievable contrast for this bg. If target is unreachable,
       go straight to the best pole (no wasted search).
    3. Otherwise, binary search for the minimum blend toward the best pole
       that achieves the target contrast ratio. Searches on the actual ratio
       (not luminance) to handle 8-bit quantization correctly.
    """
    if CONFIG.auto_contrast <= 0:
        return fg
    target = CONFIG.auto_contrast * 0.09  # 50→4.5, 100→9.0
    if _contrast_ratio(fg, bg) >= target:
        return fg

    # Check ceiling — if the best pole can't reach the target, just return it
    max_cr, pole = _max_contrast(bg)
    if max_cr <= target:
        return pole

    # Binary search for the minimum blend factor that meets the target ratio.
    # Contrast increases monotonically as we blend toward the pole.
    lo, hi = 0.0, 1.0
    for _ in range(20):
        mid = (lo + hi) / 2.0
        if _contrast_ratio(_lerp_color(fg, pole, mid), bg) >= target:
            hi = mid
        else:
            lo = mid
    return _lerp_color(fg, pole, hi)


def _gradient_t(dist: int, total_on_side: int, section: ColorSection) -> float:
    """Compute the interpolation parameter for a tab at *dist* from active."""
    if dist == 0:
        return 0.0
    if section.ramp_length > 0:
        raw = min(dist, section.ramp_length) / section.ramp_length
    else:
        denom = max(total_on_side, 1)
        raw = min(dist / denom, 1.0)
    if section.curve == Curve.POW:
        return math.pow(raw, section.exponent)
    return raw


def _section_color(
    section: ColorSection,
    dd: DrawData,
    dist: int,
    total_on_side: int,
    active_fallback: Color,
    inactive_fallback: Color,
) -> Color:
    active = section.active_color or active_fallback
    inactive = section.inactive_color or inactive_fallback
    if dist == 0:
        return active
    if section.type == ColorType.SOLID:
        return inactive
    t = _gradient_t(dist, total_on_side, section)
    return _lerp_color(active, inactive, t)


def _tab_bg(dd: DrawData, tab: TabBarData, dist: int, total_on_side: int) -> Color:
    return _section_color(
        CONFIG.background, dd, dist, total_on_side,
        dd.active_bg, dd.inactive_bg,
    )


def _tab_fg(dd: DrawData, tab: TabBarData, dist: int, total_on_side: int) -> Color:
    return _section_color(
        CONFIG.foreground, dd, dist, total_on_side,
        dd.active_fg, dd.inactive_fg,
    )

# ---------------------------------------------------------------------------
# Layout state
# ---------------------------------------------------------------------------

_active: dict[int, int] = {}
_tab_tokens: dict[int, list[list[_Token]]] = {}
_pad_budgets: dict[int, list[int]] = {}
_ideal_widths: dict[int, list[int]] = {}
_tab_count: dict[int, int] = {}
_tab_bg_cache: dict[int, list[Color]] = {}

# ---------------------------------------------------------------------------
# Chrome helpers
# ---------------------------------------------------------------------------


def _chrome_width(is_first_tab: bool) -> int:
    # first tab: leading space + trailing space + arrow = 3
    # other tabs: trailing space + arrow = 2
    return 3 if is_first_tab else 2

# ---------------------------------------------------------------------------
# draw_tab
# ---------------------------------------------------------------------------


def draw_tab(
    draw_data: DrawData,
    screen: Screen,
    tab: TabBarData,
    before: int,
    max_tab_length: int,
    index: int,
    is_last: bool,
    extra_data: ExtraData,
) -> int:
    ow = draw_data.os_window_id

    # ------------------------------------------------------------------
    # Layout pass
    # ------------------------------------------------------------------
    if extra_data.for_layout:
        if tab.is_active:
            _active[ow] = index

        _title, tokens = _prepare_title(tab, index, draw_data)
        text_width = _tokens_width(tokens)
        pad_markers = _tokens_pad_count(tokens)
        chrome = _chrome_width(index == 1)

        if pad_markers > 0:
            total_pad = max(CONFIG.pad_ideal_width - chrome - text_width, 0)
        else:
            total_pad = 0

        ideal_w = text_width + chrome + total_pad

        if index == 1:
            _tab_tokens[ow] = []
            _ideal_widths[ow] = []
            _pad_budgets[ow] = []
            _tab_count[ow] = 0

        _tab_tokens[ow].append(tokens)
        _ideal_widths[ow].append(ideal_w)
        _pad_budgets[ow].append(total_pad)
        _tab_count[ow] += 1

        if is_last:
            # Round-robin shrink padding until tabs fit
            budgets = _pad_budgets[ow]
            excess = sum(_ideal_widths[ow]) - screen.columns
            while excess > 0:
                changed = False
                for i in range(len(budgets)):
                    if excess <= 0:
                        break
                    if budgets[i] > 0:
                        budgets[i] -= 1
                        _ideal_widths[ow][i] -= 1
                        excess -= 1
                        changed = True
                if not changed:
                    break

        screen.cursor.x = before + min(ideal_w, max_tab_length)
        return screen.cursor.x

    # ------------------------------------------------------------------
    # Draw pass
    # ------------------------------------------------------------------
    active_idx = _active.get(ow, 1)
    num_tabs = _tab_count.get(ow, 1)
    dist = abs(index - active_idx)

    # Total tabs on this side of active (for gradient spanning)
    if index <= active_idx:
        total_on_side = active_idx - 1
    else:
        total_on_side = num_tabs - active_idx

    default_bg = _c(draw_data.default_bg)

    bg_color = _tab_bg(draw_data, tab, dist, total_on_side)
    fg_color = _auto_contrast_fg(
        _tab_fg(draw_data, tab, dist, total_on_side), bg_color)
    tab_bg = _c(bg_color)
    tab_fg = _c(fg_color)

    # Cache bg for arrow coloring
    if index == 1:
        _tab_bg_cache[ow] = []
    _tab_bg_cache[ow].append(bg_color)

    # Next tab bg for separator
    if extra_data.next_tab:
        nd = 0 if extra_data.next_tab.is_active else abs(index + 1 - active_idx)
        if index + 1 <= active_idx:
            next_total = active_idx - 1
        else:
            next_total = num_tabs - active_idx
        next_bg_color = _tab_bg(draw_data, extra_data.next_tab, nd, next_total)
        next_bg = _c(next_bg_color)
    else:
        next_bg = default_bg

    # Arrows point toward active tab
    use_left = (index == active_idx - 1) or (index > active_idx)

    screen.cursor.bg = tab_bg
    screen.cursor.fg = tab_fg

    # Leading space (first tab only)
    if screen.cursor.x == 0:
        screen.draw(' ')

    # Get tokens and pad budget for this tab
    tab_idx = index - 1
    all_tokens = _tab_tokens.get(ow, [])
    if tab_idx < len(all_tokens):
        tokens = all_tokens[tab_idx]
    else:
        # Fallback: re-prepare
        _, tokens = _prepare_title(tab, index, draw_data)

    budgets = _pad_budgets.get(ow, [])
    total_pad = budgets[tab_idx] if tab_idx < len(budgets) else 0
    pad_markers = _tokens_pad_count(tokens)

    # Distribute pad budget across %P markers
    if pad_markers > 0:
        per_pad = total_pad // pad_markers
        remainder = total_pad % pad_markers
    else:
        per_pad = 0
        remainder = 0

    # Draw tokens
    chrome = _chrome_width(index == 1)
    content_limit = max_tab_length - chrome
    content_start = screen.cursor.x
    pad_idx = 0

    for token in tokens:
        if token.type == _TokenType.PAD:
            this_pad = per_pad + (1 if pad_idx < remainder else 0)
            pad_idx += 1
            if this_pad > 0:
                screen.draw(CONFIG.pad_char * this_pad)
        else:
            draw_attributed_string(token.value, screen)

    # Overflow rollback: if content overran, truncate with ellipsis
    content_used = screen.cursor.x - content_start
    if content_used > content_limit:
        overshoot = content_used - content_limit
        target = screen.cursor.x - overshoot - 1
        if target > content_start:
            screen.cursor.x = target
            screen.cursor.bg = tab_bg
            screen.cursor.fg = tab_fg
            screen.draw('…')
        else:
            screen.cursor.x = content_start
            screen.cursor.bg = tab_bg
            screen.cursor.fg = tab_fg

    # Trailing space
    screen.cursor.bg = tab_bg
    screen.cursor.fg = tab_fg
    screen.draw(' ')

    # Separator arrow
    if use_left:
        screen.cursor.fg = next_bg
        screen.cursor.bg = tab_bg
        screen.draw(LEFT_ARROW)
    else:
        screen.cursor.fg = tab_bg
        screen.cursor.bg = next_bg
        screen.draw(RIGHT_ARROW)

    end = screen.cursor.x

    # Prep cursor for next tab's leading space
    screen.cursor.bg = next_bg
    screen.cursor.fg = 0
    screen.cursor.bold = False
    screen.cursor.italic = False
    if end < screen.columns:
        screen.draw(' ')

    return end


# ---------------------------------------------------------------------------
# Self-contained tests (python3 tab_bar.py --test)
# ---------------------------------------------------------------------------

def _run_tests() -> None:
    import tempfile
    import textwrap

    passed = 0
    failed = 0

    def check(name: str, got: object, expected: object) -> None:
        nonlocal passed, failed
        if got == expected:
            passed += 1
        else:
            failed += 1
            print(f'  FAIL {name}: got {got!r}, expected {expected!r}')

    # -- Tokenizer ----------------------------------------------------------
    print('tokenizer...')

    tokens = _tokenize('hello')
    check('plain text count', len(tokens), 1)
    check('plain text value', tokens[0].value, 'hello')
    check('plain text width', tokens[0].width, 5)

    tokens = _tokenize('%P')
    check('single pad', len(tokens), 1)
    check('pad type', tokens[0].type, _TokenType.PAD)

    tokens = _tokenize('%Phello%P')
    check('pad-text-pad count', len(tokens), 3)
    check('pad-text-pad types', [t.type for t in tokens],
          [_TokenType.PAD, _TokenType.TEXT, _TokenType.PAD])

    tokens = _tokenize('100%%done')
    check('escaped percent', len(tokens), 3)
    check('escaped percent text', ''.join(t.value for t in tokens), '100%done')

    tokens = _tokenize('\x1b[31mred\x1b[0m')
    check('sgr text count', len(tokens), 1)
    check('sgr text width', tokens[0].width, 3)
    check('sgr preserves escapes', '\x1b[31m' in tokens[0].value, True)

    tokens = _tokenize('\x1b[1m%Pbold%P\x1b[0m')
    check('sgr+pad count', len(tokens), 5)
    check('sgr+pad types', [t.type for t in tokens],
          [_TokenType.TEXT, _TokenType.PAD, _TokenType.TEXT, _TokenType.PAD, _TokenType.TEXT])

    # -- Token measurement --------------------------------------------------
    print('token measurement...')

    tokens = _tokenize('abc%Pdef')
    check('width excludes pad', _tokens_width(tokens), 6)
    check('pad count', _tokens_pad_count(tokens), 1)

    tokens = _tokenize('%P%P%P')
    check('all pads width', _tokens_width(tokens), 0)
    check('all pads count', _tokens_pad_count(tokens), 3)

    # -- Config parsing -----------------------------------------------------
    print('config parsing...')

    with tempfile.NamedTemporaryFile(mode='w', suffix='.toml', delete=False) as f:
        f.write(textwrap.dedent('''\
            auto_contrast=50
            [tab]
            format="%P{t}%P"
            [pad]
            ideal_width=30
            char="·"
            [background]
            type="gradient"
            active_color="#ff0000"
            inactive_color="#000000"
            ramp_length=3
            curve="pow"
            exponent=2.0
            [foreground]
            type="solid"
            active_color="#ffffff"
            inactive_color="#888888"
        '''))
        tmp = f.name

    global _config_path, CONFIG
    old_path = _config_path
    _config_path = Path(tmp)
    cfg = _load_config()
    _config_path = old_path
    os.unlink(tmp)

    check('tab_format', cfg.tab_format, '%P{t}%P')
    check('auto_contrast', cfg.auto_contrast, 50)
    check('pad_ideal_width', cfg.pad_ideal_width, 30)
    check('pad_char', cfg.pad_char, '·')
    check('bg type', cfg.background.type, ColorType.GRADIENT)
    check('bg active', cfg.background.active_color, Color(255, 0, 0))
    check('bg inactive', cfg.background.inactive_color, Color(0, 0, 0))
    check('bg ramp', cfg.background.ramp_length, 3)
    check('bg curve', cfg.background.curve, Curve.POW)
    check('bg exponent', cfg.background.exponent, 2.0)
    check('fg type', cfg.foreground.type, ColorType.SOLID)
    check('fg active', cfg.foreground.active_color, Color(255, 255, 255))
    check('fg inactive', cfg.foreground.inactive_color, Color(136, 136, 136))

    # -- Gradient math ------------------------------------------------------
    print('gradient math...')

    sec = ColorSection(
        type=ColorType.GRADIENT,
        active_color=Color(100, 100, 100),
        inactive_color=Color(0, 0, 0),
        ramp_length=0,
        curve=Curve.LINEAR,
    )

    check('gradient t=0 at active', _gradient_t(0, 4, sec), 0.0)
    check('gradient t linear mid', _gradient_t(2, 4, sec), 0.5)
    check('gradient t linear end', _gradient_t(4, 4, sec), 1.0)

    sec_ramp = ColorSection(
        type=ColorType.GRADIENT,
        ramp_length=2,
        curve=Curve.LINEAR,
    )
    check('ramp clamp', _gradient_t(5, 10, sec_ramp), 1.0)
    check('ramp mid', _gradient_t(1, 10, sec_ramp), 0.5)

    sec_pow = ColorSection(
        type=ColorType.GRADIENT,
        ramp_length=0,
        curve=Curve.POW,
        exponent=2.0,
    )
    check('pow curve', _gradient_t(1, 2, sec_pow), 0.25)

    # -- Lerp ---------------------------------------------------------------
    print('lerp...')

    check('lerp black-white 0', _lerp_color(Color(0, 0, 0), Color(255, 255, 255), 0.0), Color(0, 0, 0))
    check('lerp black-white 1', _lerp_color(Color(0, 0, 0), Color(255, 255, 255), 1.0), Color(255, 255, 255))
    mid = _lerp_color(Color(0, 0, 0), Color(200, 100, 50), 0.5)
    check('lerp midpoint', mid, Color(100, 50, 25))

    # -- Chrome width -------------------------------------------------------
    print('chrome...')
    check('first tab chrome', _chrome_width(True), 3)
    check('other tab chrome', _chrome_width(False), 2)

    # -- Luminance & contrast -----------------------------------------------
    print('luminance & contrast...')

    black = Color(0, 0, 0)
    white = Color(255, 255, 255)

    check('luminance black', round(_srgb_luminance(black), 4), 0.0)
    check('luminance white', round(_srgb_luminance(white), 4), 1.0)

    cr = _contrast_ratio(white, black)
    check('contrast white/black', round(cr, 1), 21.0)
    check('contrast symmetric', _contrast_ratio(black, white), cr)

    same = _contrast_ratio(Color(128, 128, 128), Color(128, 128, 128))
    check('contrast same', round(same, 1), 1.0)

    # -- Max contrast --------------------------------------------------------
    print('max contrast...')

    max_cr, pole = _max_contrast(black)
    check('max contrast on black', round(max_cr, 1), 21.0)
    check('best pole for black bg', pole, white)

    max_cr, pole = _max_contrast(white)
    check('max contrast on white', round(max_cr, 1), 21.0)
    check('best pole for white bg', pole, black)

    mid_gray = Color(128, 128, 128)
    max_cr, pole = _max_contrast(mid_gray)
    check('mid-gray best pole is black', pole, black)
    check('mid-gray max cr ~5.3', max_cr > 5.0 and max_cr < 5.5, True)

    # Worst-case gray (~117) has lowest max contrast
    worst_gray = Color(117, 117, 117)
    max_cr_worst, _ = _max_contrast(worst_gray)
    check('worst gray max cr ~4.6', max_cr_worst > 4.5 and max_cr_worst < 4.8, True)

    # -- Auto-contrast adjustment -------------------------------------------
    print('auto-contrast...')

    # With auto_contrast=0, no adjustment
    old_ac = CONFIG.auto_contrast
    CONFIG = Config(auto_contrast=0)
    dark_fg = Color(30, 30, 30)
    dark_bg = Color(20, 20, 20)
    check('ac=0 no change', _auto_contrast_fg(dark_fg, dark_bg), dark_fg)

    # With auto_contrast=50, target is 4.5:1
    CONFIG = Config(auto_contrast=50)
    adjusted = _auto_contrast_fg(dark_fg, dark_bg)
    ratio = _contrast_ratio(adjusted, dark_bg)
    check('ac=50 meets 4.5:1', ratio >= 4.5, True)
    # Should have pushed toward white (bg is dark)
    check('ac=50 lightened', adjusted.red > dark_fg.red, True)

    # Already-good contrast should not be changed
    bright_fg = Color(255, 255, 255)
    check('ac=50 no change if ok', _auto_contrast_fg(bright_fg, dark_bg), bright_fg)

    # With auto_contrast=100, target is 9:1
    CONFIG = Config(auto_contrast=100)
    mid_fg = Color(120, 120, 120)
    achievable_bg = Color(40, 40, 40)  # dark enough that white gives >9:1
    adjusted = _auto_contrast_fg(mid_fg, achievable_bg)
    ratio = _contrast_ratio(adjusted, achievable_bg)
    check('ac=100 meets 9:1', ratio >= 9.0, True)

    # Light bg should push fg toward black
    CONFIG = Config(auto_contrast=50)
    light_bg = Color(240, 240, 240)
    light_fg = Color(200, 200, 200)
    adjusted = _auto_contrast_fg(light_fg, light_bg)
    check('ac=50 darkened on light bg', adjusted.red < light_fg.red, True)
    ratio = _contrast_ratio(adjusted, light_bg)
    check('ac=50 light bg meets 4.5:1', ratio >= 4.5, True)

    # -- Gray-on-gray scenarios ---------------------------------------------
    print('gray-on-gray...')

    # Achievable: 4.5:1 on mid-gray (max ~5.3:1) — should find a solution
    CONFIG = Config(auto_contrast=50)
    gray_fg = Color(150, 150, 150)
    adjusted = _auto_contrast_fg(gray_fg, mid_gray)
    ratio = _contrast_ratio(adjusted, mid_gray)
    check('gray 4.5:1 met', ratio >= 4.49, True)
    check('gray 4.5:1 not excessive', ratio < 6.0, True)  # should be close to target

    # Unreachable: 9:1 on mid-gray — should return best pole (black)
    CONFIG = Config(auto_contrast=100)
    adjusted = _auto_contrast_fg(gray_fg, mid_gray)
    check('gray 9:1 unreachable → pole', adjusted, black)

    # Unreachable: 9:1 on worst-case gray (~117) — should return best pole
    # White barely beats black on this bg (4.61 vs 4.56)
    adjusted = _auto_contrast_fg(gray_fg, worst_gray)
    _, expected_pole = _max_contrast(worst_gray)
    check('worst gray 9:1 → pole', adjusted, expected_pole)

    # Bright fg on mid-gray should go toward black, not white
    CONFIG = Config(auto_contrast=50)
    bright_on_gray = Color(200, 200, 200)
    adjusted = _auto_contrast_fg(bright_on_gray, mid_gray)
    ratio = _contrast_ratio(adjusted, mid_gray)
    check('bright-on-gray meets 4.5:1', ratio >= 4.49, True)
    # Should have darkened, not lightened — black gives better contrast on this bg
    max_cr_w = _contrast_ratio(white, mid_gray)
    max_cr_b = _contrast_ratio(black, mid_gray)
    if max_cr_b > max_cr_w:
        check('bright-on-gray went dark', adjusted.red < bright_on_gray.red, True)

    # Sweep across gradient: verify all bgs from 0-255 get valid contrast
    CONFIG = Config(auto_contrast=50)
    sweep_ok = True
    sweep_worst_cr = 21.0
    sweep_worst_bg = 0
    for v in range(0, 256, 8):
        bg_test = Color(v, v, v)
        fg_test = Color(187, 187, 187)  # typical kitty inactive fg
        adj = _auto_contrast_fg(fg_test, bg_test)
        cr = _contrast_ratio(adj, bg_test)
        max_possible, _ = _max_contrast(bg_test)
        # Should meet target OR be at the max achievable
        if cr < 4.5 and cr < max_possible - 0.01:
            sweep_ok = False
            sweep_worst_cr = cr
            sweep_worst_bg = v
        if cr < sweep_worst_cr:
            sweep_worst_cr = cr
            sweep_worst_bg = v
    check('sweep: all bgs at max or target', sweep_ok, True)
    print(f'  sweep worst: bg=({sweep_worst_bg},{sweep_worst_bg},{sweep_worst_bg}) cr={sweep_worst_cr:.2f}')

    # Restore
    CONFIG = Config(auto_contrast=old_ac) if old_ac else Config()

    # -- Summary ------------------------------------------------------------
    print(f'\n{passed} passed, {failed} failed')
    sys.exit(1 if failed else 0)


if _STANDALONE and '--test' in sys.argv:
    _run_tests()
