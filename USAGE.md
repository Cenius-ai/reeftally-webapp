# ReefTally usage walkthrough

Every command below is copy-pasteable, in order, in an empty directory. Replace `python3` with your
interpreter if it is named differently.

## 1. Start a survey

```bash
python3 reeftally.py init survey.json --site "Hornsby Reef" --date 2024-06-01 \
    --observer "R. Okafor" --depth 6 --transect-length 10
```

```
created project /home/you/reef/survey.json
  site            : Hornsby Reef
  date            : 2024-06-01
  observer        : R. Okafor
  depth           : 6 m
  transect length : 10 m
  quadrats        : 0

next: reeftally add survey.json --substrate hard=12,sand=3 --coral healthy=8,pale=1,bleached=3,dead=0 --species Acro=6
```

`--date` defaults to today. Running `init` again over an existing file is refused unless you pass
`--force`; the old file is not touched.

## 2. Record quadrats

Flags (one line per quadrat, scriptable and pipe-friendly):

```bash
python3 reeftally.py add survey.json --substrate hard=12,sand=3 \
    --coral healthy=8,pale=1,bleached=3,dead=0 --species Acro=6,Poci=4 \
    --notes "starter colonies, one turbo snail scar"
```

```
added q1 (substrate 15, 12 coral colonies, 2 species)
```

* Quadrat numbers are automatic (`q1`, `q2`, ...). `--label A-07` overrides the displayed name.
* `--substrate` may be repeated, and each value may itself be a comma-separated list.
* Any flag you omit falls back to an interactive prompt when stdin is a terminal; with stdin piped
  (a script, CI) the tool refuses to prompt and tells you which flag to pass.
* Counts of `0` are accepted and recorded for coral conditions (they are structural); zero substrate
  or species counts are dropped as "not observed".

Interactive entry looks like this:

```
$ python3 reeftally.py add survey.json
Substrate tallies — one CATEGORY=COUNT per line, blank line to finish.
Categories: hard, soft, dead, algae, rubble, sand, other
  substrate> hard=12
  substrate> sand=3
  substrate>
Coral colony condition counts — press Enter to record 0.
  healthy> 8
  pale> 1
  bleached> 3
  dead>
Species tallies — one CODE=COUNT per line, blank line to finish.
  species> Acro=6
  species> Poci=4
  species>
added q1 (substrate 15, 12 coral colonies, 2 species)
```

## 3. Read the report

```bash
python3 reeftally.py report survey.json
```

```
ReefTally transect report
=========================

  Project         : survey.json
  Site            : Hornsby Reef
  Date            : 2024-06-01
  Observer        : R. Okafor
  Depth           : 6 m
  Transect length : 10 m
  Quadrat count   : 3

Substrate tallies (per quadrat)

Quadrat  hard  soft  dead  algae  rubble  sand  other  total
------------------------------------------------------------
q1         12     0     0      0       0     3      0     15
q2          9     0     0      0       6     0      0     15
A-07        6     0     0      0       2     8      0     16

Coral condition and bleaching

Quadrat   healthy  pale  bleached  dead coral  live  bleached %      band
----------------------------------------------------------------------
q1              8     1         3           0    12       25.00  moderate
q2              2     1         4           1     7       57.14    severe
A-07            0     0         2           4     2      100.00    severe
TRANSECT       10     2         9           5    21       42.86    severe

Bleaching % = bleached / (healthy + pale + bleached) x 100; recently dead colonies
are recorded but excluded from the denominator.
Transect % uses summed colony counts (9 / 21), not the mean of the quadrat percentages (60.71%).
Bands: none = 0.00%, mild = 0.01-10.00%, moderate = 10.01-30.00%, severe = > 30.00%.

Species abundance

Code             Total  Quadrats  Relative %
--------------------------------------------
Acro (unnamed)      10         3       71.43
Poci (unnamed)       4         1       28.57
```

With no quadrats recorded yet the command prints one line and exits 0:

```
no quadrats recorded yet — run: reeftally add survey.json
```

## 4. Export CSV

```bash
python3 reeftally.py report survey.json --csv out/
```

```
wrote /home/you/reef/out/quadrats.csv (new file)
wrote /home/you/reef/out/bleaching.csv (new file)
wrote /home/you/reef/out/species.csv (new file)
```

* `out/` is created if it does not exist; re-running overwrites and says `overwrote existing`.
* Headers reuse the terminal column names verbatim, and percentages are written to two decimals.
* `bleaching.csv` has one row per quadrat plus a `TRANSECT` row.

```csv
Quadrat,healthy,pale,bleached,dead coral,live,bleached %,band
q1,8,1,3,0,12,25.00,moderate
q2,2,1,4,1,7,57.14,severe
A-07,0,0,2,4,2,100.00,severe
TRANSECT,10,2,9,5,21,42.86,severe
```

## 5. Name species codes

```bash
python3 reeftally.py species set survey.json Acro=Acropora
python3 reeftally.py species set survey.json "Monti=Montipora cf. digitata"
python3 reeftally.py species list survey.json
```

```
set Acro -> Acropora
Species codes recorded in survey.json

Code   Name                    Quadrats  Observations
-----------------------------------------------------
Acro   Acropora                       3            10
Poci   unnamed                        1             4
Monti  Montipora cf. digitata         0             0
```

Names flow straight into the abundance table and `species.csv` (`Acro (Acropora)`). Codes you never
named stay visible as `(unnamed)` rather than being rejected. `species set survey.json Acro=` clears
a name again.

## 6. Correct the last quadrat

```bash
python3 reeftally.py remove survey.json
```

```
about to delete A-07 (substrate 16, 6 coral colonies, 1 species)
remove A-07? [y/N] n
nothing removed
```

Answering anything but `y` leaves the file byte-identical and exits 0. `--yes` skips the prompt:

```bash
python3 reeftally.py remove survey.json --yes
removed A-07
```

On a project with no quadrats: `error: no quadrats to remove` on stderr, exit 2.

## 7. When something is wrong

Every validation failure is written to stderr, exits 2, and leaves the project file exactly as it
was:

```
$ python3 reeftally.py add survey.json --substrate hard=-3
error: invalid count for 'hard': '-3' (counts must be whole numbers >= 0)

$ python3 reeftally.py add survey.json --substrate hrad=3
error: unknown substrate category 'hrad': valid categories are hard, soft, dead, algae, rubble, sand, other

$ python3 reeftally.py add survey.json --species Acro
error: invalid --species pair 'Acro': expected CODE=COUNT (example: --species Acro=6,Poci=4)

$ python3 reeftally.py report broken.json
error: broken.json: invalid JSON at line 1 column 2: Expecting property name enclosed in double quotes

$ python3 reeftally.py report missing.json
error: missing.json: project file not found — run: reeftally init missing.json --site "Hornsby Reef"

$ python3 reeftally.py frobnicate
unknown command: frobnicate
run 'reeftally --help' to see the available commands
```

A project written by a newer ReefTally is refused rather than half-read:

```
error: survey.json: project schema_version 2 is newer than this tool (1)
```
