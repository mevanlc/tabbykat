# tabbykat

A custom [kitty](https://sw.kovidgoyal.net/kitty/) tab bar renderer with:

- Gradient "spotlight" background centered on the active tab
- WCAG-based auto-contrast with perceptual bias controls
- Customizeable via a structured `tab_bar.toml` config
- Powerline-style tab dovetailing

![](https://i.imgur.com/j8st8ah.png)

<img src="https://i.imgur.com/jaSmo0G.gif" width="100%">

## Install

tabbykat is loaded by kitty as a `tab_bar_style custom` module.

1. Copy `tab_bar.py` and `tab_bar.toml` into your kitty config dir
   (typically `~/.config/kitty/`).
2. Add to `kitty.conf`:

   ```conf
   tab_bar_style custom
   ```

3. Reload kitty (`ctrl+shift+F5`) or restart it.

Run the self-contained test suite with:

```sh
python3 tab_bar.py --test
```

## Configure

All configuration lives in `tab_bar.toml` alongside `tab_bar.py`. The file is
re-read on kitty config reload.

### Top-level contrast controls

| Key              | Purpose                                                                  |
|------------------|--------------------------------------------------------------------------|
| `auto_contrast`  | 0=off, 50=WCAG AA, ~78=AAA, 100≈9:1, 234+=pure B/W                       |
| `contrast_bias`  | >1 biases dark-on-light stricter, light-on-dark looser (1.0=symmetric)   |
| `pole_bias`      | >1 prefers darkening fg; <1 prefers lightening (1.0=natural crossover)   |

### `[tab]` — title format

`format` is a Python `str.format()` template. Placeholders:

| Token                       | Meaning                                               |
|-----------------------------|-------------------------------------------------------|
| `{n}` / `{n:03d}`           | Tab number (supports format specs)                    |
| `{nu}`                      | Tab number in superscript (⁰¹²³⁴⁵⁶⁷⁸⁹)                 |
| `{t}`                       | Tab title (with `…/` → `~/` normalization)            |
| `{exe}`                     | Foreground process basename (e.g. `vim`, `python3`)   |
| `{fmt.fg.*}` / `{fmt.bg.*}` | Kitty colors: named, hex (`_RRGGBB`), `tab`, `default`|
| `{fmt.bold}` / `{fmt.italic}` + `no*` variants | Attribute toggles                  |
| `{fmt.reset}`               | Reset all attributes                                  |
| `%P`                        | Padding distribution point (repeatable; split evenly) |
| `%%`                        | Literal `%`                                           |

Example: `format="{fmt.fg._444444}{nu}{fmt.fg.tab} {t}%P"`

### `[[transform]]` - regex replacements

Add one top-level table per rule:

```toml
[[transform]]
s='^/Users/[^/]+/'
r='~/'
c='t'

[[transform]]
s=' \| '
r=' / '
f='g'
```

| Key | Meaning | Default |
|-----|---------|---------|
| `s` | Python regex to search for | Required |
| `r` | Replacement, including Python backreferences such as `\1` or `\g<name>` | `''` (delete matches) |
| `f` | Regex flags and replacement count | `''` (replace first match) |
| `c` | Component: `n`, `nu`, `t`, `exe`, or `*` for the whole string | `'*'` |

Flags `a`, `i`, `m`, `s`, `u`, and `x` map to Python's `re.A`, `re.I`,
`re.M`, `re.S`, `re.U`, and `re.X`. Unicode matching is already the default.
`l` (`re.L`) is rejected because it requires bytes patterns.
Digits `1` through `9` set the maximum replacement count; `g` and `0` replace
all matches. The last count flag wins: `g2` means two, `23` means three,
and `2g` means all. With no count flag, only the first match is replaced.

Component rules run after each placeholder's conversion and format spec:
`c='n'` sees `007` for `{n:03d}` at tab 7. Repeated placeholders are transformed
independently, and changing `n` does not change `nu` or the actual tab index.
Title normalization happens before transforms.

All component rules run first, then whole-string rules, preserving declaration
order within each stage. Whole-string rules see the expanded string, including
ANSI styling sequences and `%P`/`%%` markers, before contrast adjustment,
padding calculation, and truncation. They can therefore change those controls.
Invalid rules are skipped with an indexed diagnostic on config load; other
rules and settings still apply.

### `[pad]` — tab width

| Key            | Purpose                                              |
|----------------|------------------------------------------------------|
| `ideal_width`  | Target tab width in cells (shrinks when crowded)     |
| `char`         | Padding character                                    |

### `[background]` and `[foreground]` — section colors

Only apply where the format hasn't set an explicit color (e.g. `{fmt.fg.tab}`).

| Key                              | Purpose                                              |
|----------------------------------|------------------------------------------------------|
| `type`                           | `"gradient"` or `"solid"`                            |
| `active_color` / `inactive_color`| Named, hex, or `""` for kitty's default              |
| `ramp_min` / `ramp_max`          | Min/max gradient span in cells (0 = no bound)        |
| `curve`                          | `"linear"` or `"pow"`                                |
| `exponent`                       | Curve exponent when `curve="pow"`                    |

### `[log]`

| Key     | Purpose                                              |
|---------|------------------------------------------------------|
| `file`  | Log path; empty disables logging                     |
| `level` | `"off"`, `"error"`, `"warn"`, `"info"`, `"debug"`    |

See `tab_bar.toml` for inline documentation and defaults, and
`devdocs/TABBYKAT.md` for architecture and internals.

## License

MIT — see [LICENSE](LICENSE).
