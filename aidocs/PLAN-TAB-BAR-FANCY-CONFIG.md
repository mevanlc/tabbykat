# tabbykat — Fancy Tab Bar Config Plan

## Goal

Refactor [tab_bar.py](/Users/mclark/.config/kitty/tab_bar.py) so the fancy custom tab bar is driven entirely by the structured schema in [tab_bar.toml](/Users/mclark/.config/kitty/tab_bar.toml).

This is a hard refactor:

- no backward compatibility with the current flat TOML reader
- no migration layer
- no legacy constants as the source of truth once the refactor is complete

## Confirmed Decisions

- Adopt the existing structured TOML schema fully: `[tab]`, `[pad]`, `[background]`, `[foreground]`.
- `[tab].format` becomes the single source of truth for tab text rendering.
- `%P` keeps the current padding distribution behavior. Example: `%P{t}%P` should emulate centered title padding without tab numbers.
- Directional arrows remain. Their colors are derived from adjacent tab colors:
  - right-facing arrow: `fg = left tab fg-adjacent color`, `bg = right tab bg-adjacent color`
  - left-facing arrow: `fg = right tab fg-adjacent color`, `bg = left tab bg-adjacent color`
- `ramp_length = 0` means interpolate across the full visible distance from the active tab to the edge in each direction.
- Nonzero `ramp_length` means interpolate for that many steps, then clamp at the edge color.
- Config is only re-read when kitty reloads the custom tab bar module.

## Scope

Implement:

- TOML parsing and validation for the structured schema
- tab text formatting via `[tab].format`
- `%P`-based padding distribution using `[pad]`
- configurable background and foreground color models
- directional arrow coloring based on adjacent rendered tabs
- overflow handling that preserves padding priority and falls back to kitty-style truncation

Do not implement:

- backward compatibility for the older flat TOML keys
- migration docs
- dynamic config re-reading without a kitty reload

## Config Model

### `[tab]`

`format` is the authoritative per-tab content template.

Supported placeholders:

- `{n}` for tab number using Python `str.format()`
- `{t}` for display title after title normalization such as `…/foo -> ~/foo`

Kitty color/style placeholders (same syntax as `tab_title_template`):

- `{fmt.fg.<name>}` set foreground to a named CSS/X11 color, hex (`{fmt.fg._FF0000}`), `tab` (tab's own color), or `default`
- `{fmt.bg.<name>}` same variants for background
- `{fmt.bold}` / `{fmt.nobold}` bold on/off
- `{fmt.italic}` / `{fmt.noitalic}` italic on/off
- `{fmt.reset}` reset all attributes

Custom escapes:

- `%P` padding distribution point
- `%%` literal `%`

These are the only custom escapes. All color and style control uses kitty's native `{fmt.*}` system, which produces `\x1b[...m` SGR sequences that `draw_attributed_string()` already handles.

Plan detail:

- resolve `str.format()` with `n`, `t`, and a kitty `Formatter()` instance as `fmt`
- the result is a string containing plain text, SGR escape sequences, and `%P` / `%%` markers
- tokenize that result into: text+SGR segments and `%P` markers
- allow multiple `%P` markers and split total pad evenly across them
- when there is remainder padding, assign extra cells left-to-right across `%P` markers

### `[pad]`

- `ideal_width` is the target total tab width before kitty-level truncation
- `char` is the fill character used at each `%P` marker; must have `wcswidth(char) == 1` (reject double-width, zero-width, or combining characters and fall back to space)

Plan detail:

- compute each tab’s rendered text width with `%P` removed
- compute desired total padding from `ideal_width - fixed_rendered_width`
- store desired per-tab pad budgets during the layout pass
- if total ideal widths exceed `screen.columns`, reduce pad budgets round-robin across tabs, subtracting one cell from one `%P` budget contribution at a time until the tabs fit or all padding is exhausted

### `[background]` and `[foreground]`

Each section supports:

- `type = "solid"` or `type = "gradient"`
- `active_color`
- `inactive_color`
- `ramp_length`
- `curve = "linear"` or `curve = "pow"`
- `exponent`

Plan detail:

- section colors only apply where the tab format uses `{fmt.fg.tab}` / `{fmt.bg.tab}` or has no explicit color set
- explicit `{fmt.fg.*}` / `{fmt.bg.*}` escapes in the format override section-derived colors for the span where they are active
- empty section colors mean “fall back to kitty's default tab colors” for that channel

## Rendering Model

### 1. Title Preparation

For each tab:

- normalize the displayed title with the existing `…/ -> ~/` logic
- produce `{n}` and `{t}` substitution values
- bind `ColorFormatter.draw_data` and `ColorFormatter.tab_data` for the current tab so that `{fmt.fg.tab}` / `{fmt.bg.tab}` resolve to the correct tab colors
- evaluate `[tab].format` via `str.format(n=..., t=..., fmt=Formatter())` to produce a string containing plain text, SGR escapes, and `%P` markers; wrap in try/except and fall back to the raw title on any formatting error
- tokenize that string into text+SGR segments and `%P` markers

### 2. Layout Pass

For each tab in `extra_data.for_layout`:

- use the pre-built token list without actual screen drawing
- measure visible width of text tokens via `wcswidth()` (SGR escapes have zero width; `%P` markers are structural)
- calculate desired total padding from `[pad].ideal_width`
- distribute desired padding across `%P` markers
- add fixed chrome width for the renderer:
  - first tab: leading space + trailing space + arrow
  - later tabs: trailing space + arrow
- store:
  - token list (text+SGR segments and `%P` markers)
  - `%P` marker count
  - ideal width

After all tabs are measured:

- compare sum of ideal widths to `screen.columns`
- reduce padding round-robin across tabs until the total fits or every tab has zero distributed padding
- preserve at least one title cell if a tab still has any title content

### 3. Draw Pass

For each tab:

- compute background and foreground colors for that tab based on active distance and the section config
- render leading space for the first tab
- render each token:
  - text+SGR segments via `draw_attributed_string()`
  - `%P` expansions as pad chars using the reduced padding budget for that tab
- apply kitty-style overflow rollback after drawing the content region
- draw the trailing space
- draw the directional arrow using adjacent tab colors

### 4. Arrow Coloring

Preserve the current directional-arrow behavior and make coloring explicit:

- right-facing arrow:
  - arrow `fg` comes from the tab on the left side of the arrow
  - arrow `bg` comes from the tab on the right side of the arrow
- left-facing arrow:
  - arrow `fg` comes from the tab on the right side of the arrow
  - arrow `bg` comes from the tab on the left side of the arrow

This should be based on the actual rendered tab edge colors, not inferred from the arrow direction alone.

## Gradient Semantics

Distance is measured from the active tab separately on the left and right sides.

If `ramp_length == 0`:

- interpolate from active color to inactive color across the full number of visible tabs to the left edge and separately across the full number of visible tabs to the right edge
- clamp naturally at the last visible tab on that side

If `ramp_length > 0`:

- interpolate for `min(distance, ramp_length)` steps
- any tab beyond `ramp_length` uses the same clamped edge color as the tab at `distance == ramp_length`

Curves:

- `linear`: direct proportional interpolation
- `pow`: raise normalized distance to `exponent` before interpolation

## Implementation Steps

1. Replace the current flat `Config` object with structured config dataclasses matching the TOML sections.
2. Add validated enums for section types and curves; use kitty's `Color.parse_color()` / `to_color()` for section color parsing.
3. Introduce a tokenizer that splits a format-expanded string (containing plain text, SGR escapes, and `%P` markers) into drawable segments and pad markers.
4. Refactor title preparation to bind `ColorFormatter.draw_data` and `ColorFormatter.tab_data` for the current tab, then evaluate `str.format(n=..., t=..., fmt=Formatter())` and tokenize the result. Catch formatting exceptions and fall back to the raw title string.
5. Refactor the layout pass to measure text token widths via `wcswidth()` (excluding `%P` and SGR) and compute per-tab pad budgets from `[pad].ideal_width`.
6. Rework padding reduction so it shrinks distributed `%P` budgets round-robin across tabs before any truncation logic runs.
7. Refactor background and foreground color selection to use the section configs, including `solid`, `gradient`, `ramp_length`, and curve behavior.
8. Refactor the draw pass to render text+SGR tokens via `draw_attributed_string()` and `%P` tokens as pad chars, replacing the current `draw_title(...)` call.
9. Restore explicit overflow rollback after drawing content so kitty-style truncation happens after padding is exhausted.
10. Update directional arrow drawing to derive colors from the neighboring rendered tabs according to the agreed rules.
11. Remove the current partial flat-key config support and dead constants that are replaced by TOML-driven behavior.

## Verification

Manual verification should cover:

- `%P{t}%P` centers titles with symmetric padding when space allows
- padding shrinks round-robin as tabs are added
- once all padding is gone, titles truncate with ellipsis instead of disappearing
- single-character and very short titles still keep at least one visible glyph
- tab numbering works through `{n}` formatting, including zero-padded examples
- `{fmt.fg.tab}` / `{fmt.bg.tab}` correctly expose section-derived colors
- explicit `{fmt.fg.*}` / `{fmt.bg.*}` color escapes override section colors locally
- `{fmt.bold}` / `{fmt.italic}` and their resets work within the format
- `background.type = "solid"` and `foreground.type = "solid"` behave as fixed colors
- `gradient` with `ramp_length = 0` spans all the way to each edge from the active tab
- `gradient` with `ramp_length > 0` clamps after the configured number of steps
- both `linear` and `pow` curves produce visibly distinct ramps
- left-facing and right-facing arrows adopt colors from the correct adjacent tabs
- a typo in the format string (e.g. `{fmt.fg.reed}`) falls back gracefully instead of crashing
- a double-width `[pad].char` is rejected and replaced with space
- config changes take effect after kitty config reload

## Robustness

Since the refactor removes all legacy fallbacks and makes `[tab].format` the sole rendering path, config errors must not take down the tab bar.

- **Format expansion**: wrap `str.format()` in try/except. On any error (bad placeholder, unmatched brace, invalid color name like `{fmt.fg.reed}`), fall back to rendering the raw title string with no formatting. Log the error once per config reload via `log_error()`.
- **Formatter binding**: before each `str.format()` call, bind `ColorFormatter.draw_data` and `ColorFormatter.tab_data` so that `{fmt.fg.tab}` / `{fmt.bg.tab}` resolve correctly. Failure to bind would cause `AttributeError` at expansion time, caught by the same try/except.
- **Pad char validation**: at config load, verify `wcswidth(pad_char) == 1`. If it fails (double-width, zero-width, combining), fall back to space. The layout math assumes one pad unit = one cell and this invariant must hold.

## Main Risk

The highest-risk part is mixing three layers of styling correctly:

- kitty’s cursor fg/bg state (set via `screen.cursor.fg`/`.bg`)
- explicit inline SGR sequences from `{fmt.*}` expansions in the format (applied via `screen.apply_sgr()`)
- fallback section colors from `[background]` and `[foreground]`

Using kitty’s native `{fmt.*}` system and `draw_attributed_string()` reduces this risk compared to a custom color escape system — SGR application is handled by kitty’s own code. The remaining complexity is ensuring section-derived colors are set on the cursor *before* drawing each token, so that `{fmt.fg.tab}` resolves to the gradient/solid color for that tab’s position.
