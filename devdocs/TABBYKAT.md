# tabbykat — Institutional Knowledge

Reference for agents working on `tab_bar.py` and `tab_bar.toml`.

## What it is

A custom kitty tab bar renderer (`tab_bar_style custom`) with:
- Gradient "spotlight" background centered on the active tab
- Directional powerline arrows pointing toward the active tab
- WCAG-based auto-contrast with perceptual bias controls
- Fully driven by a structured `tab_bar.toml` config

## Architecture

### File layout

- `tab_bar.py` — the renderer, loaded by kitty at startup and on config reload
- `tab_bar.toml` — all config; re-read when kitty reloads the module
- `aidocs/PLAN-TAB-BAR-FANCY-CONFIG.md` — original design plan (may be stale on details; this file is more current)

### Kitty integration points

- Kitty calls `draw_tab()` with the exact signature defined by `DrawTabFunc` in `kitty/tab_bar.py`
- `draw_tab()` is called twice per render cycle: once with `extra_data.for_layout = True` (measure widths, no drawing), then again for actual drawing
- Config reload: `kill -SIGUSR1 <kitty_pid>` re-imports the module, re-executing `CONFIG = _load_config()` at module level
- The module is loaded via `runpy.run_path()` — it's NOT a normal import. `__file__` is set, `__name__` is `__main__` equivalent

### Key kitty APIs we use

| API | Source | Purpose |
|-----|--------|---------|
| `Color(r, g, b)` | `kitty.fast_data_types` | C extension type, not a NamedTuple. Has `.red`, `.green`, `.blue`, `.luminance`, `.as_sgr` |
| `wcswidth(str)` | `kitty.fast_data_types` | C-level Unicode-aware string width |
| `Screen` | `kitty.fast_data_types` | Drawing surface. `.cursor.fg`, `.cursor.bg` (int), `.cursor.x`, `.draw(str)`, `.apply_sgr(str)`, `.columns` |
| `as_rgb(int)` | `kitty.tab_bar` | Encodes 24-bit color int for `cursor.fg`/`.bg`: `(color_int << 8) \| 2` |
| `color_as_int(Color)` | `kitty.utils` | `Color` → 24-bit packed int |
| `draw_attributed_string(str, Screen)` | `kitty.tab_bar` | Draws text with embedded `\x1b[...m` SGR sequences, splitting and calling `screen.apply_sgr()` |
| `ColorFormatter` / `Formatter` | `kitty.tab_bar` | Produces SGR escape sequences for `{fmt.fg.red}`, `{fmt.bg.tab}`, etc. Class-level `.draw_data` and `.tab_data` must be bound before use |
| `DrawData.tab_fg(tab)` / `.tab_bg(tab)` | `kitty.tab_bar` | Resolves tab color, checking per-tab overrides (`tab.active_fg` etc.) first |
| `TabBarData._replace()` | `kitty.tab_bar` | NamedTuple; use `._replace(inactive_fg=...)` to inject per-tab color overrides |
| `get_boss().tab_for_id(id)` | `kitty.fast_data_types` | Access the actual Tab object for `get_exe_of_active_window()` etc. |

### Reference source

`~/p/gh/kitty` contains a clone of the kitty source. Key files:
- `kitty/tab_bar.py` — `ColorFormatter`, `Formatter`, `DrawData`, `TabBarData`, `ExtraData`, `draw_attributed_string`, `as_rgb`, built-in draw_tab implementations
- `kitty/fast_data_types.pyi` — type stubs for `Color`, `Screen`, `wcswidth`
- `kitty/rgb.py` — `to_color()`, `color_as_sgr()`, `color_from_int()`, `alpha_blend()`
- `kitty/utils.py` — `color_as_int()`, `sgr_sanitizer_pat()`

## Gotchas learned the hard way

### SGR separator: colons not semicolons

Kitty's `ColorFormatter` produces ISO 8613-6 format: `\x1b[38:2:R:G:Bm` (colon-separated), NOT the older `\x1b[38;2;R;G;Bm`. Any regex matching SGR sequences must accept both. Our `_FG_24BIT` regex uses `[;:]` for this.

### {fmt.fg.tab} bakes colors into tokens at expansion time

The format string is expanded via `str.format()` which calls `ColorFormatter.__getattr__('tab')`, resolving to the tab's fg/bg color as an SGR escape *at that moment*. If you expand during the layout pass, the colors are the original (non-auto-contrasted) values.

**Fix**: In the draw pass, re-prepare the title with:
1. A modified `TabBarData` with auto-contrasted fg injected as per-tab overrides (`._replace(inactive_fg=...)`)
2. `bg_for_contrast` passed to `_prepare_title()` so `_contrast_adjust_sgr()` post-processes ALL inline fg SGRs

### ColorFormatter uses class-level state

`ColorFormatter.draw_data` and `ColorFormatter.tab_data` are **class attributes** shared across the `Formatter.fg` and `Formatter.bg` instances. You must bind them before every `str.format()` call. They're not instance attributes — `Formatter.fg` and `Formatter.bg` are class-level `ColorFormatter` objects on `Formatter`.

### Auto-contrast: the gray zone is physics, not a bug

Mid-gray backgrounds (~RGB 117) have a maximum achievable WCAG contrast ratio of ~4.6:1 against any foreground color. This is a mathematical ceiling — no algorithm can exceed it. The `_max_contrast()` function detects this and returns the best pole directly.

Reference libs confirming this: `~/p/gh/wcag-contrast-ratio`, `~/p/gh/color-contrast`, `~/p/gh/chroma.js`.

### contrast_bias direction must be based on pole, not input fg

The bias (boost dark-on-light, dampen light-on-dark) must check which *pole* will be used, not whether the original fg is darker than bg. The original fg might be lighter than bg but get pushed to black — the result is dark-on-light and needs the bias.

### 8-bit quantization matters for contrast targeting

Binary searching for a target *luminance* then rounding to 8-bit RGB can land just below the target contrast ratio. Always binary search on the **actual contrast ratio** of the quantized color.

### Layout pass vs draw pass

- **Layout** (`extra_data.for_layout`): advance `screen.cursor.x` to the tab's end position. No drawing. Token widths are measured here, pad budgets computed. Called for ALL tabs before any draw calls.
- **Draw**: render to screen. Re-prepare tokens here (not cached from layout) because auto-contrast and color overrides depend on the gradient position which wasn't known during layout.

### screen.cursor.fg encoding

`screen.cursor.fg` and `.bg` are ints, NOT `Color` objects. Encode with `as_rgb(color_as_int(color))`. The low byte `2` is a tag indicating 24-bit RGB mode. `cursor.fg = 0` means terminal default, not black.

## Testing

`python3 tab_bar.py --test` runs the self-contained test suite (90+ tests) using shimmed kitty types. Tests cover:
- Tokenizer (text, SGR, %P, %%)
- Config parsing (all sections, validation, fallbacks)
- Gradient math (ramp_min, ramp_max, pow curves)
- Color lerp
- WCAG luminance and contrast ratio
- Auto-contrast (ceiling detection, binary search, pole selection)
- SGR contrast adjustment (semicolons, colons, bg untouched)
- Contrast bias (dark-on-light boost, light-on-dark dampening)
- Gray-on-gray sweep (all bg values 0–255 meet target or ceiling)

## Format placeholders

| Placeholder | Description |
|-------------|-------------|
| `{n}` | Tab number (supports format specs like `{n:03d}`) |
| `{nu}` | Tab number in superscript (⁰¹²³⁴⁵⁶⁷⁸⁹) |
| `{t}` | Tab title (with `…/` → `~/` normalization) |
| `{exe}` | Foreground process basename (lazy — only resolved when present in format) |
| `{fmt.fg.*}` | Kitty native fg color (named, hex `_RRGGBB`, `tab`, `default`) |
| `{fmt.bg.*}` | Kitty native bg color |
| `{fmt.bold}` / `{fmt.nobold}` | Bold on/off |
| `{fmt.italic}` / `{fmt.noitalic}` | Italic on/off |
| `{fmt.reset}` | Reset all attributes |
| `%P` | Padding distribution point |
| `%%` | Literal `%` |
