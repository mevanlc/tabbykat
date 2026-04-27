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
_FG_24BIT = re.compile(r'\033\[38[;:]2[;:](\d+)[;:](\d+)[;:](\d+)m')

# ---------------------------------------------------------------------------
# Config model
# ---------------------------------------------------------------------------


class ColorType(Enum):
    SOLID = 'solid'
    GRADIENT = 'gradient'


class Curve(Enum):
    LINEAR = 'linear'
    POW = 'pow'


class Interpolation(Enum):
    RGB = 'rgb'
    OKLCH = 'oklch'


@dataclass(frozen=True)
class ColorSection:
    type: ColorType = ColorType.SOLID
    active_color: Color | None = None
    inactive_color: Color | None = None
    ramp_min: int = 0   # gradient spans at least this many steps
    ramp_max: int = 0   # gradient spans at most this many steps; 0 = no limit
    curve: Curve = Curve.LINEAR
    exponent: float = 1.0
    interpolation: Interpolation = Interpolation.RGB


class LogLevel(Enum):
    OFF = 'off'
    ERROR = 'error'
    WARN = 'warn'
    INFO = 'info'
    DEBUG = 'debug'


_LOG_RANK = {
    LogLevel.OFF: 0, LogLevel.ERROR: 1, LogLevel.WARN: 2,
    LogLevel.INFO: 3, LogLevel.DEBUG: 4,
}


@dataclass(frozen=True)
class LogConfig:
    file: str = ''
    level: LogLevel = LogLevel.OFF


@dataclass(frozen=True)
class Config:
    tab_format: str = '{t}'
    pad_ideal_width: int = 24
    pad_char: str = ' '
    auto_contrast: int = 0  # 0=off, 50=WCAG AA (4.5:1), 100=(9:1), 234+=pure B/W
    contrast_bias: float = 1.0  # multiplier on target for dark-on-light; >1 boosts
    pole_bias: float = 1.0  # >1 prefers darkening, <1 prefers lightening
    log: LogConfig = field(default_factory=LogConfig)
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

    raw_interp = data.get('interpolation', 'rgb')
    try:
        interp = Interpolation(raw_interp)
    except ValueError:
        interp = Interpolation.RGB

    ramp_min = data.get('ramp_min', 0)
    if not isinstance(ramp_min, int):
        ramp_min = 0
    ramp_min = max(0, ramp_min)

    ramp_max = data.get('ramp_max', 0)
    if not isinstance(ramp_max, int):
        ramp_max = 0
    ramp_max = max(0, ramp_max)

    exp = data.get('exponent', 1.0)
    if not isinstance(exp, (int, float)):
        exp = 1.0
    exp = max(0.0, float(exp))

    return ColorSection(
        type=ctype,
        active_color=_parse_color(data.get('active_color', '')),
        inactive_color=_parse_color(data.get('inactive_color', '')),
        ramp_min=ramp_min,
        ramp_max=ramp_max,
        curve=curve,
        exponent=exp,
        interpolation=interp,
    )


def _parse_log_config(data: dict) -> LogConfig:
    raw_file = data.get('file', '')
    if not isinstance(raw_file, str):
        raw_file = ''
    raw_file = os.path.expanduser(raw_file.strip())

    raw_level = data.get('level', 'off')
    if not isinstance(raw_level, str):
        raw_level = 'off'
    try:
        level = LogLevel(raw_level.strip().lower())
    except ValueError:
        level = LogLevel.OFF

    return LogConfig(file=raw_file, level=level)


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
    auto_contrast = max(0, int(auto_contrast))

    contrast_bias = data.get('contrast_bias', 1.0)
    if not isinstance(contrast_bias, (int, float)):
        contrast_bias = 1.0
    contrast_bias = max(0.0, float(contrast_bias))

    pole_bias = data.get('pole_bias', 1.0)
    if not isinstance(pole_bias, (int, float)):
        pole_bias = 1.0
    pole_bias = max(0.01, float(pole_bias))

    return Config(
        tab_format=tab_format,
        pad_ideal_width=ideal_width,
        pad_char=pad_char,
        auto_contrast=auto_contrast,
        contrast_bias=contrast_bias,
        pole_bias=pole_bias,
        log=_parse_log_config(data.get('log', {})),
        background=_parse_color_section(data.get('background', {})),
        foreground=_parse_color_section(data.get('foreground', {})),
    )


CONFIG = _load_config()


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _log(level: LogLevel, msg: str) -> None:
    if not CONFIG.log.file or CONFIG.log.level == LogLevel.OFF:
        return
    if _LOG_RANK[level] > _LOG_RANK[CONFIG.log.level]:
        return
    try:
        with open(CONFIG.log.file, 'a') as f:
            f.write(f'[{level.value}] {msg}\n')
    except OSError:
        pass


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
_SUPERSCRIPT_DIGITS = str.maketrans('0123456789', '⁰¹²³⁴⁵⁶⁷⁸⁹')


def _superscript(n: int) -> str:
    return str(n).translate(_SUPERSCRIPT_DIGITS)


def _fix_title(title: str) -> str:
    if title.startswith('…/'):
        candidate = _home + title[1:]
        if os.path.exists(candidate):
            return '~' + title[1:]
    return title


def _contrast_adjust_sgr(expanded: str, bg: Color) -> str:
    """Auto-contrast all inline 24-bit fg color SGR sequences against bg."""
    if CONFIG.auto_contrast <= 0:
        return expanded

    def _replace(m: re.Match) -> str:
        fg = Color(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        adj = _auto_contrast_fg(fg, bg)
        _log(LogLevel.DEBUG,
             f'  sgr_adjust: ({fg.red},{fg.green},{fg.blue})->({adj.red},{adj.green},{adj.blue})'
             f' on bg=({bg.red},{bg.green},{bg.blue}) cr={_contrast_ratio(adj, bg):.2f}')
        # Preserve the original separator style (colon or semicolon)
        sep = m.group(0)[4]  # char after '\x1b[38'
        return f'\x1b[38{sep}2{sep}{adj.red}{sep}{adj.green}{sep}{adj.blue}m'

    return _FG_24BIT.sub(_replace, expanded)


def _prepare_title(
    tab: TabBarData, index: int, draw_data: DrawData,
    bg_for_contrast: Color | None = None,
) -> tuple[str, list[_Token]]:
    """Expand the format string and tokenize it.

    If *bg_for_contrast* is provided, all inline fg colors in the expanded
    string are auto-contrasted against it.
    """
    title = _fix_title(tab.title)

    # Bind ColorFormatter so {fmt.fg.tab} / {fmt.bg.tab} resolve correctly
    ColorFormatter.draw_data = draw_data
    ColorFormatter.tab_data = tab

    # Resolve foreground exe if needed (lazy — only calls get_boss if {exe} is in format)
    if _STANDALONE:
        exe = ''
    elif '{exe' in CONFIG.tab_format:
        try:
            from kitty.fast_data_types import get_boss
            ktab = get_boss().tab_for_id(tab.tab_id)
            exe = os.path.basename((ktab.get_exe_of_active_window() if ktab else '') or '')
        except Exception:
            exe = ''
    else:
        exe = ''

    try:
        expanded = CONFIG.tab_format.format(n=index, nu=_superscript(index), t=title, exe=exe, fmt=_fmt)
    except Exception:
        log_error(f'tab_bar.toml: bad [tab].format, falling back to title')
        expanded = title

    if bg_for_contrast is not None:
        expanded = _contrast_adjust_sgr(expanded, bg_for_contrast)

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


# OKLab / OKLCH (Björn Ottosson, 2020). sRGB ↔ linear ↔ OKLab ↔ OKLCH.

def _srgb_decode(v: int) -> float:
    s = v / 255.0
    return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4


def _srgb_encode(v: float) -> int:
    v = max(0.0, min(1.0, v))
    s = 12.92 * v if v <= 0.0031308 else 1.055 * (v ** (1 / 2.4)) - 0.055
    return max(0, min(255, round(s * 255)))


def _rgb_to_oklab(c: Color) -> tuple[float, float, float]:
    r = _srgb_decode(c.red)
    g = _srgb_decode(c.green)
    b = _srgb_decode(c.blue)
    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l_ = l ** (1 / 3)
    m_ = m ** (1 / 3)
    s_ = s ** (1 / 3)
    return (
        0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
        1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
        0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_,
    )


def _oklab_to_rgb(L: float, a: float, b: float) -> Color:
    l_ = L + 0.3963377774 * a + 0.2158037573 * b
    m_ = L - 0.1055613458 * a - 0.0638541728 * b
    s_ = L - 0.0894841775 * a - 1.2914855480 * b
    l = l_ ** 3
    m = m_ ** 3
    s = s_ ** 3
    r = +4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s
    g = -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s
    bl = -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s
    return Color(_srgb_encode(r), _srgb_encode(g), _srgb_encode(bl))


def _rgb_to_oklch(c: Color) -> tuple[float, float, float]:
    L, a, b = _rgb_to_oklab(c)
    return L, math.hypot(a, b), math.atan2(b, a)


def _oklch_to_rgb(L: float, C: float, H: float) -> Color:
    return _oklab_to_rgb(L, C * math.cos(H), C * math.sin(H))


def _lerp_color_oklch(a: Color, b: Color, t: float) -> Color:
    """Interpolate in OKLCH with shortest-arc hue."""
    L1, C1, H1 = _rgb_to_oklch(a)
    L2, C2, H2 = _rgb_to_oklch(b)
    L = L1 + (L2 - L1) * t
    C = C1 + (C2 - C1) * t
    # If either endpoint is near-neutral, hue is undefined — adopt the other's.
    if C1 < 1e-4:
        H = H2
    elif C2 < 1e-4:
        H = H1
    else:
        diff = H2 - H1
        if diff > math.pi:
            diff -= 2 * math.pi
        elif diff < -math.pi:
            diff += 2 * math.pi
        H = H1 + diff * t
    return _oklch_to_rgb(L, C, H)


def _lerp_section(section: ColorSection, a: Color, b: Color, t: float) -> Color:
    if section.interpolation == Interpolation.OKLCH:
        return _lerp_color_oklch(a, b, t)
    return _lerp_color(a, b, t)


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
    """Return (max_ratio, best_pole) for a given background.

    pole_bias > 1 favors black (darkening), < 1 favors white (lightening).
    The bias multiplies the black pole's score before comparison, shifting
    the crossover point. The returned ratio is always the true (unbiased) max.
    """
    bg_lum = _srgb_luminance(bg)
    white_cr = (1.0 + 0.05) / (bg_lum + 0.05)
    black_cr = (bg_lum + 0.05) / (0.0 + 0.05)
    # Bias the comparison, not the returned ratio
    if black_cr * CONFIG.pole_bias >= white_cr:
        return black_cr, Color(0, 0, 0)
    return white_cr, Color(255, 255, 255)


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
    # Bias: boost dark-on-light (pole=black → *bias), dampen light-on-dark (pole=white → /bias)
    max_cr, pole = _max_contrast(bg)
    if CONFIG.contrast_bias != 1.0:
        if pole.red == 0:
            target *= CONFIG.contrast_bias
        else:
            target /= CONFIG.contrast_bias
    if _contrast_ratio(fg, bg) >= target:
        return fg

    # If the best pole can't reach the target, just return it
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
    # Effective span: start with actual tabs on this side
    span = total_on_side
    if section.ramp_min > 0:
        span = max(span, section.ramp_min)
    if section.ramp_max > 0:
        span = min(span, section.ramp_max)
    denom = max(span, 1)
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
    return _lerp_section(section, active, inactive, t)


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
    raw_fg = _tab_fg(draw_data, tab, dist, total_on_side)
    fg_color = _auto_contrast_fg(raw_fg, bg_color)
    _, log_pole = _max_contrast(bg_color)
    base_target = CONFIG.auto_contrast * 0.09
    if CONFIG.contrast_bias != 1.0:
        eff_target = base_target * CONFIG.contrast_bias if log_pole.red == 0 else base_target / CONFIG.contrast_bias
    else:
        eff_target = base_target
    _log(LogLevel.DEBUG,
         f'tab={index} dist={dist} bg=({bg_color.red},{bg_color.green},{bg_color.blue})'
         f' raw_fg=({raw_fg.red},{raw_fg.green},{raw_fg.blue})'
         f' adj_fg=({fg_color.red},{fg_color.green},{fg_color.blue})'
         f' cr={_contrast_ratio(fg_color, bg_color):.2f}'
         f' pole={"B" if log_pole.red == 0 else "W"} bias={"*" if log_pole.red == 0 else "/"}{CONFIG.contrast_bias} target={eff_target:.1f}')
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

    # Re-prepare tokens in draw pass: inject auto-contrasted fg into TabBarData
    # so {fmt.fg.tab} resolves correctly, and pass bg so ALL inline fg colors
    # (including explicit ones like {fmt.fg._444444}) get auto-contrasted too.
    fg_int = color_as_int(fg_color)
    tab_with_contrast = tab._replace(
        active_fg=fg_int if tab.is_active else tab.active_fg,
        inactive_fg=fg_int if not tab.is_active else tab.inactive_fg,
    )
    _, tokens = _prepare_title(tab_with_contrast, index, draw_data,
                               bg_for_contrast=bg_color)

    tab_idx = index - 1

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
            ramp_min=2
            ramp_max=5
            curve="pow"
            exponent=2.0
            interpolation="oklch"
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
    check('bg ramp_min', cfg.background.ramp_min, 2)
    check('bg ramp_max', cfg.background.ramp_max, 5)
    check('bg curve', cfg.background.curve, Curve.POW)
    check('bg exponent', cfg.background.exponent, 2.0)
    check('bg interpolation', cfg.background.interpolation, Interpolation.OKLCH)
    check('fg interpolation default rgb', cfg.foreground.interpolation, Interpolation.RGB)
    check('fg type', cfg.foreground.type, ColorType.SOLID)
    check('fg active', cfg.foreground.active_color, Color(255, 255, 255))
    check('fg inactive', cfg.foreground.inactive_color, Color(136, 136, 136))

    # -- Gradient math ------------------------------------------------------
    print('gradient math...')

    # Both 0: span exactly the actual tabs
    sec = ColorSection(type=ColorType.GRADIENT, curve=Curve.LINEAR)
    check('gradient t=0 at active', _gradient_t(0, 4, sec), 0.0)
    check('gradient t linear mid', _gradient_t(2, 4, sec), 0.5)
    check('gradient t linear end', _gradient_t(4, 4, sec), 1.0)

    # ramp_max: hard cap at 2 steps, 10 actual tabs
    sec_max = ColorSection(type=ColorType.GRADIENT, ramp_max=2, curve=Curve.LINEAR)
    check('ramp_max clamp', _gradient_t(5, 10, sec_max), 1.0)
    check('ramp_max mid', _gradient_t(1, 10, sec_max), 0.5)

    # ramp_min: stretch to 8 steps even though only 3 actual tabs
    sec_min = ColorSection(type=ColorType.GRADIENT, ramp_min=8, curve=Curve.LINEAR)
    check('ramp_min stretch', _gradient_t(3, 3, sec_min), 3 / 8)
    check('ramp_min at edge', _gradient_t(8, 3, sec_min), 1.0)
    # With fewer than ramp_min tabs, gradient doesn't reach endpoint
    check('ramp_min partial', _gradient_t(2, 3, sec_min), 0.25)

    # ramp_min + ramp_max: min=3, max=6, actual=1 → span=3
    sec_both = ColorSection(type=ColorType.GRADIENT, ramp_min=3, ramp_max=6, curve=Curve.LINEAR)
    check('min+max few tabs', _gradient_t(1, 1, sec_both), 1 / 3)
    # actual=10 → span=6 (max wins)
    check('min+max many tabs', _gradient_t(6, 10, sec_both), 1.0)
    check('min+max mid', _gradient_t(3, 10, sec_both), 0.5)

    # ramp_min > ramp_max: max is hard cap
    sec_conflict = ColorSection(type=ColorType.GRADIENT, ramp_min=10, ramp_max=2, curve=Curve.LINEAR)
    check('min>max: max wins', _gradient_t(2, 5, sec_conflict), 1.0)
    check('min>max: mid', _gradient_t(1, 5, sec_conflict), 0.5)

    # pow curve (both 0 = span actual)
    sec_pow = ColorSection(type=ColorType.GRADIENT, curve=Curve.POW, exponent=2.0)
    check('pow curve', _gradient_t(1, 2, sec_pow), 0.25)

    # -- Lerp ---------------------------------------------------------------
    print('lerp...')

    check('lerp black-white 0', _lerp_color(Color(0, 0, 0), Color(255, 255, 255), 0.0), Color(0, 0, 0))
    check('lerp black-white 1', _lerp_color(Color(0, 0, 0), Color(255, 255, 255), 1.0), Color(255, 255, 255))
    mid = _lerp_color(Color(0, 0, 0), Color(200, 100, 50), 0.5)
    check('lerp midpoint', mid, Color(100, 50, 25))

    # -- OKLCH interpolation ------------------------------------------------
    print('oklch...')

    # Round-trip: rgb → oklab → rgb should be near-identity
    for c in (Color(255, 0, 0), Color(0, 255, 0), Color(0, 0, 255),
              Color(123, 200, 50), Color(0, 0, 0), Color(255, 255, 255)):
        L, a, b = _rgb_to_oklab(c)
        rt = _oklab_to_rgb(L, a, b)
        check(f'oklab roundtrip {c}',
              abs(rt.red - c.red) <= 1 and abs(rt.green - c.green) <= 1 and abs(rt.blue - c.blue) <= 1,
              True)

    # Endpoints unchanged at t=0 and t=1
    green = Color(0, 200, 0)
    blue = Color(0, 0, 200)
    rt0 = _lerp_color_oklch(green, blue, 0.0)
    rt1 = _lerp_color_oklch(green, blue, 1.0)
    check('oklch t=0 is start',
          abs(rt0.red - green.red) <= 1 and abs(rt0.green - green.green) <= 1 and abs(rt0.blue - green.blue) <= 1,
          True)
    check('oklch t=1 is end',
          abs(rt1.red - blue.red) <= 1 and abs(rt1.green - blue.green) <= 1 and abs(rt1.blue - blue.blue) <= 1,
          True)

    # OKLCH midpoint between green and blue stays chromatic (passes through cyan-ish),
    # whereas RGB midpoint of (0,200,0)→(0,0,200) is (0,100,100) — much darker/duller.
    mid_rgb = _lerp_color(green, blue, 0.5)
    mid_oklch = _lerp_color_oklch(green, blue, 0.5)
    L_rgb, C_rgb, _ = _rgb_to_oklch(mid_rgb)
    L_oklch, C_oklch, _ = _rgb_to_oklch(mid_oklch)
    check('oklch midpoint more chromatic than rgb', C_oklch > C_rgb, True)
    # And OKLCH lightness lies between the endpoints (monotonic ramp).
    L_green, _, _ = _rgb_to_oklch(green)
    L_blue, _, _ = _rgb_to_oklch(blue)
    lo, hi = sorted((L_green, L_blue))
    check('oklch midpoint L between endpoints', lo - 1e-3 <= L_oklch <= hi + 1e-3, True)

    # Hue takes the shortest arc: green (~142°) → blue (~264°) goes through cyan,
    # not yellow→red→magenta. Check the midpoint hue lies in (142°, 264°).
    _, _, H_mid = _rgb_to_oklch(mid_oklch)
    H_mid_deg = math.degrees(H_mid) % 360
    check('oklch hue takes shortest arc', 140 < H_mid_deg < 270, True)

    # Near-neutral endpoint: hue should be borrowed from the chromatic side.
    gray = Color(128, 128, 128)
    red = Color(220, 30, 30)
    out = _lerp_color_oklch(gray, red, 0.5)
    # Should be a desaturated red, not e.g. green or blue
    check('oklch neutral→red midpoint is reddish', out.red > out.green and out.red > out.blue, True)

    # Section dispatch: OKLCH section uses oklch lerp
    sec_rgb = ColorSection(type=ColorType.GRADIENT, interpolation=Interpolation.RGB,
                           active_color=green, inactive_color=blue)
    sec_oklch = ColorSection(type=ColorType.GRADIENT, interpolation=Interpolation.OKLCH,
                             active_color=green, inactive_color=blue)
    out_rgb = _lerp_section(sec_rgb, green, blue, 0.5)
    out_oklch = _lerp_section(sec_oklch, green, blue, 0.5)
    check('section rgb matches _lerp_color', out_rgb, _lerp_color(green, blue, 0.5))
    check('section oklch matches _lerp_color_oklch', out_oklch, _lerp_color_oklch(green, blue, 0.5))
    check('section rgb ≠ section oklch', out_rgb != out_oklch, True)

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

    # -- SGR contrast adjustment -----------------------------------------------
    print('sgr contrast adjust...')

    CONFIG = Config(auto_contrast=50)
    dark_bg = Color(20, 20, 20)

    # A dim fg SGR should get brightened
    sgr_in = '\x1b[38;2;40;40;40mhello'
    sgr_out = _contrast_adjust_sgr(sgr_in, dark_bg)
    # Extract the adjusted RGB from the output
    m = _FG_24BIT.search(sgr_out)
    check('sgr adjust: found SGR', m is not None, True)
    if m:
        adj_fg = Color(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        check('sgr adjust: meets target', _contrast_ratio(adj_fg, dark_bg) >= 4.5, True)
        check('sgr adjust: was brightened', adj_fg.red > 40, True)

    # Multiple SGRs in one string
    sgr_multi = '\x1b[38;2;40;40;40mnum)\x1b[38;2;50;50;50m title'
    sgr_multi_out = _contrast_adjust_sgr(sgr_multi, dark_bg)
    matches = _FG_24BIT.findall(sgr_multi_out)
    check('sgr adjust: both adjusted', len(matches), 2)
    for r, g, b in matches:
        adj = Color(int(r), int(g), int(b))
        check(f'sgr multi: ({r},{g},{b}) meets target',
              _contrast_ratio(adj, dark_bg) >= 4.5, True)

    # Colon-separated (kitty native format)
    sgr_colon = '\x1b[38:2:40:40:40mhello'
    sgr_colon_out = _contrast_adjust_sgr(sgr_colon, dark_bg)
    m = _FG_24BIT.search(sgr_colon_out)
    check('sgr adjust colon: found SGR', m is not None, True)
    if m:
        adj_fg = Color(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        check('sgr adjust colon: meets target', _contrast_ratio(adj_fg, dark_bg) >= 4.5, True)
    # Verify colon separator preserved
    check('sgr adjust colon: sep preserved', ':2:' in sgr_colon_out, True)

    # With auto_contrast=0, no adjustment
    CONFIG = Config(auto_contrast=0)
    check('sgr adjust: ac=0 passthrough', _contrast_adjust_sgr(sgr_in, dark_bg), sgr_in)

    # Non-fg SGR (background) should be untouched
    CONFIG = Config(auto_contrast=50)
    bg_sgr = '\x1b[48;2;40;40;40mhello'
    check('sgr adjust: bg untouched', _contrast_adjust_sgr(bg_sgr, dark_bg), bg_sgr)

    # -- Contrast bias ---------------------------------------------------------
    print('contrast bias...')

    # Dark-on-light with bias=1.5 should push harder than without
    light_bg_bias = Color(200, 200, 200)
    dark_fg_bias = Color(120, 120, 120)

    CONFIG = Config(auto_contrast=50, contrast_bias=1.0)
    adj_no_bias = _auto_contrast_fg(dark_fg_bias, light_bg_bias)
    cr_no_bias = _contrast_ratio(adj_no_bias, light_bg_bias)

    CONFIG = Config(auto_contrast=50, contrast_bias=1.5)
    adj_biased = _auto_contrast_fg(dark_fg_bias, light_bg_bias)
    cr_biased = _contrast_ratio(adj_biased, light_bg_bias)

    check('bias: dark-on-light boosted', cr_biased > cr_no_bias, True)

    # Light-on-dark should be dampened (target / bias)
    dark_bg_bias = Color(30, 30, 30)
    light_fg_bias = Color(120, 120, 120)

    CONFIG = Config(auto_contrast=50, contrast_bias=1.0)
    adj_no_bias_lod = _auto_contrast_fg(light_fg_bias, dark_bg_bias)
    cr_no_bias_lod = _contrast_ratio(adj_no_bias_lod, dark_bg_bias)

    CONFIG = Config(auto_contrast=50, contrast_bias=1.5)
    adj_biased_lod = _auto_contrast_fg(light_fg_bias, dark_bg_bias)
    cr_biased_lod = _contrast_ratio(adj_biased_lod, dark_bg_bias)

    check('bias: light-on-dark dampened', cr_biased_lod < cr_no_bias_lod, True)

    CONFIG = Config()

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
