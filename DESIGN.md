# ReefTally design direction (source of truth)

This file is the committed design contract for the product. Every screen (in a CLI, a screen is a
command's rendered output) consumes the tokens and the layout skeleton below. Nothing here may be
swapped for a generic slate-neutral + blue default.

## Committed direction

| Token | Value |
| --- | --- |
| Archetype | `cli` — a terminal product, no window, no server, no web view |
| Style family | calm-precise |
| Accent (one, and only one) | `oklch(0.58 0.12 152)` — hue 152 |
| Accent in hex (non-CSS contexts: badges, terminal themes, screencasts) | `#388e56` |
| Type personality | mono / mono |
| Density | compact |
| Signature | aligned columns; one accent for primary status; no color-only signal |
| Layout | aligned terminal columns; help-first output |
| Shape language | monospace, no chrome; box-drawing separators; one rule system on every surface |
| Surface / mode | terminal dark — the only honest CLI surface |
| Type family | system monospace (`monospace` on every platform; no loaded webfont, no remote asset) |

## Neutral ramp for the terminal-dark surface

| Role | Token | Contrast note |
| --- | --- | --- |
| Surface / background | `oklch(0.19 0.012 240)` | the terminal dark ground |
| Primary text | `oklch(0.93 0.006 240)` | > 12:1 on the surface |
| Secondary / muted labels | `oklch(0.74 0.010 240)` | > 6:1 on the surface |
| Rules and separators | `oklch(0.46 0.010 240)` | decorative only, never text-bearing |
| Accent (primary status) | `oklch(0.58 0.12 152)` / `#388e56` | > 4.5:1 for text-size use |

All body text sits at ≥ 4.5:1 against the surface; nothing relies on hue alone.

## How a plain-text CLI consumes this

1. **No ANSI, ever.** The plan requires output that redirects to a file and diffs cleanly, so the
   tool emits no escape sequences and no color. The accent therefore carries meaning through
   *structure*, not pigment: the **bleaching band** (`none` / `mild` / `moderate` / `severe` / `n/a`)
   is the one status-bearing column, always a word, never a color-only signal. The hex above is the
   canonical accent for any non-terminal surface (badge, screenshot, favourite palette) and is the
   value a terminal theme should map to its accent slot.
2. **Aligned columns are the layout.** Every screen is a set of auto-sized columns separated by two
   spaces, with a single rule line between header and body — the same rule system on the header
   block, the substrate table, the coral/bleaching table and the abundance table.
3. **Box-drawing separators, rendered as ASCII.** A plain-text surface uses `-` for the horizontal
   rule (and indentation instead of vertical bars) so the bytes stay identical on Linux, macOS and
   Windows and can be diffed. The shape language is respected: no boxes, no chrome, no decoration
   beyond the rule under each header.
4. **Help-first output.** A bare invocation prints the full usage plus a worked example, so the
   first thing on screen is the map of the product.
5. **Compact density.** Section title, blank line, table; no banners, no ASCII art, no progress
   decoration.
6. **System monospace only.** No downloaded font, no remote asset, no network at any point — the
   tool must run on a boat.

## Section skeleton (every screen)

```
<title>                              <- one heading naming the product or the command
=====                                <- rule, same length as the title
  <label> : <value>                  <- metadata block, labels padded to one column width

<section title>

<header cells>                       <- auto-sized to the widest cell
<rule>
<body rows>                          <- first column left-aligned, numbers right-aligned

<footnote lines>                     <- the formula being applied, stated once
```

## Empty, error and populated states

* **Populated** — real seeded content renders immediately (`examples/hornsby-reef.json`), never a
  placeholder: real site names, real survey dates, mixed severity bands and two edge quadrats.
* **Empty** — one line saying why plus the exact next command:
  `no quadrats recorded yet — run: reeftally add survey.json`;
  `no species recorded yet — add a quadrat with --species CODE=COUNT`.
* **Error** — written to stderr, exit 2, the offending token quoted, the rule restated and the
  project file left byte-identical; never a stack trace.

## Non-goals carried over from the plan

Authentication, accounts and permissions (there is no server, session or shared data to protect —
the project file path is the access boundary), multi-user editing, cloud sync, GUI/web/mobile
surfaces, photo-based scoring, GIS/GPS, full taxonomy, statistics, Excel/PDF output, configurable
thresholds, i18n and packaged installers. ReefTally is a single-user local terminal tool.
