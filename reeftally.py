#!/usr/bin/env python3
"""ReefTally - coral transect quadrat counter, bleaching calculator and species log.

ReefTally records per-quadrat substrate tallies and coral-colony condition counts,
calculates bleaching percentages per quadrat and per transect, and keeps a species
abundance log for a single reef transect survey.

Everything lives in one local JSON project file that the user passes to every
command. There is no server, no database, no network access and no third-party
dependency: the tool is standard library only and runs where it is unpacked.

    python3 reeftally.py --help
    python3 reeftally.py init survey.json --site "Hornsby Reef" --observer "R. Okafor"
    python3 reeftally.py add survey.json --substrate hard=12,sand=3 \
        --coral healthy=8,pale=1,bleached=3,dead=0 --species Acro=6,Poci=4
    python3 reeftally.py report survey.json --csv out/

Exit codes: 0 success, 2 user or validation error, 1 unexpected internal error.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

__version__ = "1.0.0"
TOOL_NAME = "ReefTally"

#: Bumped only when the on-disk layout changes. An older file is refused rather
#: than half-read; a newer file is refused outright so a newer tool keeps it.
SCHEMA_VERSION = 1

#: Fixed, documented substrate vocabulary. Refusing a typo stops the categories
#: from silently splitting into "hard" and "hrd" across a transect.
SUBSTRATE_CATEGORIES: Tuple[str, ...] = (
    "hard",
    "soft",
    "dead",
    "algae",
    "rubble",
    "sand",
    "other",
)

#: Fixed coral-colony condition vocabulary. "dead" means recently dead colonies:
#: they are recorded but excluded from the bleaching denominator.
CORAL_CONDITIONS: Tuple[str, ...] = ("healthy", "pale", "bleached", "dead")

#: Conventional, fixed bleaching severity bands (percentage of live colonies).
BLEACHING_BANDS: Tuple[Tuple[str, float, float], ...] = (
    ("none", 0.0, 0.0),
    ("mild", 0.0, 10.0),
    ("moderate", 10.0, 30.0),
    ("severe", 30.0, float("inf")),
)

CSV_QUADRAT_HEADERS: Tuple[str, ...] = (
    "Quadrat",
    "hard",
    "soft",
    "dead",
    "algae",
    "rubble",
    "sand",
    "other",
    "total",
    "healthy",
    "pale",
    "bleached",
    "dead coral",
    "live",
    "bleached %",
    "band",
    "notes",
)
CSV_BLEACHING_HEADERS: Tuple[str, ...] = (
    "Quadrat",
    "healthy",
    "pale",
    "bleached",
    "dead coral",
    "live",
    "bleached %",
    "band",
)
CSV_SPECIES_HEADERS: Tuple[str, ...] = ("Code", "Total", "Quadrats", "Relative %")

SUBSTRATE_TABLE_HEADERS: Tuple[str, ...] = ("Quadrat",) + SUBSTRATE_CATEGORIES + ("total",)
CORAL_TABLE_HEADERS: Tuple[str, ...] = (
    "Quadrat",
    "healthy",
    "pale",
    "bleached",
    "dead coral",
    "live",
    "bleached %",
    "band",
    "notes",
)
SPECIES_TABLE_HEADERS: Tuple[str, ...] = ("Code", "Total", "Quadrats", "Relative %")
SPECIES_LIST_HEADERS: Tuple[str, ...] = ("Code", "Name", "Quadrats", "Observations")

EMPTY_REPORT_HINT = "no quadrats recorded yet — run: reeftally add {path}"
EMPTY_SPECIES_HINT = "no species recorded yet — add a quadrat with --species CODE=COUNT"

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_USAGE = 2


class ReefTallyError(Exception):
    """An expected failure with a message meant for the person at the terminal."""

    exit_code = EXIT_USAGE


class UsageError(ReefTallyError):
    """Bad flags, bad counts, bad combination of arguments."""

    exit_code = EXIT_USAGE


class ProjectFileError(ReefTallyError):
    """Missing, unreadable, corrupt or unsupported project file."""

    exit_code = EXIT_USAGE


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def utc_now() -> str:
    """Return a second-resolution UTC timestamp, e.g. 2024-06-01T09:15:04Z."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def plural(count: int, singular: str, plural_form: Optional[str] = None) -> str:
    """Return the singular for one, otherwise the plural."""
    if count == 1:
        return singular
    return plural_form if plural_form is not None else singular + "s"


def format_metres(value: Optional[float]) -> str:
    """Render a metres value without trailing zeros, or an explicit 'not recorded'."""
    if value is None or value == "":
        return "(not recorded)"
    number = float(value)
    if number.is_integer():
        return "%d m" % int(number)
    return "%s m" % ("%g" % number)


def format_percent(value: Optional[float]) -> str:
    """Render a percentage to two decimals, or 'n/a' when it is undefined."""
    return "n/a" if value is None else "%.2f" % value


def bleach_band(percent: Optional[float]) -> str:
    """Map a bleaching percentage onto its fixed severity band label."""
    if percent is None:
        return "n/a"
    if percent <= 0.0:
        return "none"
    if percent <= 10.0:
        return "mild"
    if percent <= 30.0:
        return "moderate"
    return "severe"


def bleaching_percent(bleached: int, live: int) -> Optional[float]:
    """Bleaching percentage of live colonies, or None when there are none.

    The denominator is healthy + pale + bleached: recently dead colonies are
    excluded so colonies that are already gone cannot dilute the percentage.
    """
    if live <= 0:
        return None
    return round(bleached * 100.0 / live, 2)


def display_name(quadrat: Mapping[str, Any]) -> str:
    """Quadrat name shown in tables: the --label override, else 'q<number>'."""
    label = quadrat.get("label")
    if label:
        return str(label)
    return "q%s" % quadrat.get("number", 0)


def substrate_total(quadrat: Mapping[str, Any]) -> int:
    """Sum of every substrate count recorded for the quadrat."""
    return sum(int(value) for value in (quadrat.get("substrate") or {}).values())


def coral_counts(quadrat: Mapping[str, Any]) -> Dict[str, int]:
    """Return every coral condition, defaulting missing keys to zero."""
    coral = quadrat.get("coral_condition") or {}
    return {key: int(coral.get(key, 0) or 0) for key in CORAL_CONDITIONS}


def coral_total(quadrat: Mapping[str, Any]) -> int:
    """All colonies counted for the quadrat, live plus recently dead."""
    return sum(coral_counts(quadrat).values())


def live_colonies(quadrat: Mapping[str, Any]) -> int:
    """Colonies in the bleaching denominator (dead colonies excluded)."""
    counts = coral_counts(quadrat)
    return counts["healthy"] + counts["pale"] + counts["bleached"]


# --------------------------------------------------------------------------- #
# Metrics engine - derived at read time, never stored in the project file
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BleachingRow:
    """One row of the bleaching summary: a quadrat, or the whole transect."""

    scope: str
    label: str
    healthy: int
    pale: int
    bleached: int
    dead: int
    live: int
    percent: Optional[float]
    band: str
    notes: str = ""


@dataclass(frozen=True)
class AbundanceRow:
    """One species code rolled up across the whole transect."""

    code: str
    name: Optional[str]
    total_count: int
    quadrats_present: int
    relative_percent: float

    @property
    def display_code(self) -> str:
        """'Acro (Acropora)' when named, 'Acro (unnamed)' when it never was."""
        return "%s (%s)" % (self.code, self.name if self.name else "unnamed")


def bleaching_rows(project: Mapping[str, Any]) -> List[BleachingRow]:
    """Per-quadrat bleaching rows followed by the transect row.

    The transect percentage is computed from summed colony counts rather than
    the mean of the quadrat percentages, so one small quadrat cannot dominate.
    """
    rows: List[BleachingRow] = []
    totals = {key: 0 for key in CORAL_CONDITIONS}
    for quadrat in project.get("quadrats") or []:
        counts = coral_counts(quadrat)
        for key in CORAL_CONDITIONS:
            totals[key] += counts[key]
        live = counts["healthy"] + counts["pale"] + counts["bleached"]
        percent = bleaching_percent(counts["bleached"], live)
        rows.append(
            BleachingRow(
                scope="quadrat",
                label=display_name(quadrat),
                healthy=counts["healthy"],
                pale=counts["pale"],
                bleached=counts["bleached"],
                dead=counts["dead"],
                live=live,
                percent=percent,
                band=bleach_band(percent),
                notes=str(quadrat.get("notes") or ""),
            )
        )
    live_total = totals["healthy"] + totals["pale"] + totals["bleached"]
    transect_percent = bleaching_percent(totals["bleached"], live_total)
    rows.append(
        BleachingRow(
            scope="transect",
            label="TRANSECT",
            healthy=totals["healthy"],
            pale=totals["pale"],
            bleached=totals["bleached"],
            dead=totals["dead"],
            live=live_total,
            percent=transect_percent,
            band=bleach_band(transect_percent),
        )
    )
    return rows


def abundance_rows(project: Mapping[str, Any]) -> List[AbundanceRow]:
    """Species rollup: totals, quadrats present and relative abundance.

    Sorted by total descending, ties broken alphabetically by code.
    """
    names = project.get("species_names") or {}
    totals: Dict[str, int] = {}
    present: Dict[str, int] = {}
    for quadrat in project.get("quadrats") or []:
        for code, count in (quadrat.get("species") or {}).items():
            value = int(count)
            if value <= 0:
                continue
            totals[code] = totals.get(code, 0) + value
            present[code] = present.get(code, 0) + 1
    observations = sum(totals.values())
    if observations <= 0:
        return []
    rows = [
        AbundanceRow(
            code=code,
            name=names.get(code) or None,
            total_count=total,
            quadrats_present=present.get(code, 0),
            relative_percent=round(total * 100.0 / observations, 2),
        )
        for code, total in totals.items()
    ]
    rows.sort(key=lambda row: (-row.total_count, row.code))
    return rows


def mean_quadrat_percent(project: Mapping[str, Any]) -> Optional[float]:
    """Mean of the per-quadrat percentages, quoted only to show it is not used."""
    percents = []
    for quadrat in project.get("quadrats") or []:
        counts = coral_counts(quadrat)
        live = counts["healthy"] + counts["pale"] + counts["bleached"]
        percents.append(bleaching_percent(counts["bleached"], live))
    usable = [value for value in percents if value is not None]
    if not usable:
        return None
    return round(sum(usable) / len(usable), 2)


# --------------------------------------------------------------------------- #
# Fixed-width table renderer (standard library only, no formatting package)
# --------------------------------------------------------------------------- #


def render_table(
    headers: Sequence[str],
    rows: Iterable[Sequence[Any]],
    left_align: Sequence[int] = (0,),
) -> str:
    """Render an auto-sized plain-text table.

    Every column is padded to its widest cell, a rule of '-' separates the
    header from the body, and no ANSI escape sequence is emitted, so the output
    can be redirected to a file or diffed cleanly.
    """
    left = set(left_align)
    body = [[str(cell) for cell in row] for row in rows]
    widths = [len(str(header)) for header in headers]
    for row in body:
        for index, cell in enumerate(row):
            if index < len(widths):
                widths[index] = max(widths[index], len(cell))

    def render_row(row: Sequence[str]) -> str:
        parts = []
        for index, width in enumerate(widths):
            cell = row[index] if index < len(row) else ""
            parts.append(cell.ljust(width) if index in left else cell.rjust(width))
        return "  ".join(parts).rstrip()

    lines = [render_row([str(header) for header in headers])]
    lines.append("-" * (sum(widths) + 2 * (len(widths) - 1)))
    lines.extend(render_row(row) for row in body)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Project file I/O
# --------------------------------------------------------------------------- #


def new_project(
    site: str,
    survey_date: str,
    observer: str,
    depth_m: Optional[float],
    transect_length_m: Optional[float],
) -> Dict[str, Any]:
    """Build a fresh, empty project document with its schema version and stamps."""
    now = utc_now()
    return {
        "schema_version": SCHEMA_VERSION,
        "site": site,
        "date": survey_date,
        "observer": observer,
        "depth_m": depth_m,
        "transect_length_m": transect_length_m,
        "created_at": now,
        "updated_at": now,
        "quadrats": [],
        "species_names": {},
    }


def load_project(path: Path) -> Dict[str, Any]:
    """Read and validate the project file, naming the exact problem on failure."""
    if path.is_dir():
        raise ProjectFileError("%s is a directory, not a project file" % path)
    if not path.exists():
        raise ProjectFileError(
            "%s: project file not found — run: reeftally init %s --site \"Hornsby Reef\"" % (path, path)
        )
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ProjectFileError("%s: not UTF-8 text (byte offset %d)" % (path, exc.start))
    except OSError as exc:
        raise ProjectFileError("%s: cannot read project file: %s" % (path, exc.strerror or exc))

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProjectFileError(
            "%s: invalid JSON at line %d column %d: %s" % (path, exc.lineno, exc.colno, exc.msg)
        )
    if not isinstance(data, dict):
        raise ProjectFileError("%s: project file must contain a JSON object" % path)

    version = data.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ProjectFileError("%s: missing or non-integer schema_version" % path)
    if version > SCHEMA_VERSION:
        raise ProjectFileError(
            "%s: project schema_version %d is newer than this tool (%d)"
            % (path, version, SCHEMA_VERSION)
        )
    if version < SCHEMA_VERSION:
        raise ProjectFileError(
            "%s: project schema_version %d is older than this tool (%d); "
            "this version has no migration path" % (path, version, SCHEMA_VERSION)
        )

    quadrats = data.get("quadrats")
    if not isinstance(quadrats, list):
        raise ProjectFileError("%s: 'quadrats' must be a list of quadrat objects" % path)
    for index, quadrat in enumerate(quadrats, start=1):
        if not isinstance(quadrat, dict):
            raise ProjectFileError("%s: quadrat #%d is not a JSON object" % (path, index))
        if not isinstance(quadrat.get("number"), int) or isinstance(quadrat.get("number"), bool):
            raise ProjectFileError("%s: quadrat #%d has no integer 'number'" % (path, index))
        for key in ("substrate", "coral_condition", "species"):
            if key in quadrat and not isinstance(quadrat[key], dict):
                raise ProjectFileError(
                    "%s: quadrat %s: field '%s' must be an object"
                    % (path, display_name(quadrat), key)
                )
    if not isinstance(data.get("species_names") or {}, dict):
        raise ProjectFileError("%s: 'species_names' must be an object" % path)
    data.setdefault("species_names", {})
    return data


def save_project(path: Path, project: Dict[str, Any]) -> None:
    """Write the project atomically: temp file in the same directory, then replace.

    An interrupted run therefore either leaves the previous file untouched or
    replaces it completely - it can never truncate a survey in progress.
    """
    project["updated_at"] = utc_now()
    payload = json.dumps(project, indent=2, ensure_ascii=False) + "\n"
    directory = path.parent if str(path.parent) else Path(".")
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ProjectFileError(
            "%s: cannot create %s: %s" % (path, directory, exc.strerror or exc)
        )
    handle = None
    tmp_path: Optional[Path] = None
    try:
        descriptor, tmp_name = tempfile.mkstemp(
            prefix=".%s." % path.name, suffix=".tmp", dir=str(directory)
        )
        tmp_path = Path(tmp_name)
        handle = os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        handle = None
        os.replace(str(tmp_path), str(path))
        tmp_path = None
    except OSError as exc:
        raise ProjectFileError("%s: cannot write project file: %s" % (path, exc.strerror or exc))
    finally:
        if handle is not None:
            with contextlib.suppress(OSError):
                handle.close()
        if tmp_path is not None and tmp_path.exists():
            with contextlib.suppress(OSError):
                tmp_path.unlink()


# --------------------------------------------------------------------------- #
# KEY=VALUE parsing and validation
# --------------------------------------------------------------------------- #


def split_pairs(raw_values: Sequence[str], flag: str) -> List[str]:
    """Flatten repeated --flag a=1,b=2 values into individual KEY=VALUE tokens."""
    tokens: List[str] = []
    for raw in raw_values:
        text = str(raw)
        if not text.strip():
            continue
        for chunk in text.split(","):
            chunk = chunk.strip()
            if not chunk:
                raise UsageError(
                    "empty item in %s '%s': expected pairs separated by commas "
                    "(example: %s hard=12,sand=3)" % (flag, text, flag)
                )
            tokens.append(chunk)
    return tokens


def parse_count(key: str, token: str) -> int:
    """Validate a count token, quoting the offending text back at the user."""
    try:
        value = int(token, 10)
    except (TypeError, ValueError):
        raise UsageError(
            "invalid count for '%s': '%s' (counts must be whole numbers >= 0)" % (key, token)
        )
    if value < 0:
        raise UsageError(
            "invalid count for '%s': '%s' (counts must be whole numbers >= 0)" % (key, token)
        )
    return value


def split_pair(token: str, flag: str, expected: str, example: str) -> Tuple[str, str]:
    """Split one KEY=VALUE token, restating the expected syntax when malformed."""
    if "=" not in token:
        raise UsageError(
            "invalid %s pair '%s': expected %s (example: %s %s)" % (flag, token, expected, flag, example)
        )
    key, _, value = token.partition("=")
    key = key.strip()
    value = value.strip()
    if not key:
        raise UsageError(
            "invalid %s pair '%s': expected %s (example: %s %s)" % (flag, token, expected, flag, example)
        )
    return key, value


def parse_substrate_values(raw_values: Sequence[str]) -> Dict[str, int]:
    """Parse and validate --substrate pairs against the fixed category set."""
    values: Dict[str, int] = {}
    for token in split_pairs(raw_values, "--substrate"):
        key, count_text = split_pair(token, "--substrate", "CATEGORY=COUNT", "hard=12,sand=3")
        if key not in SUBSTRATE_CATEGORIES:
            raise UsageError(
                "unknown substrate category '%s': valid categories are %s"
                % (key, ", ".join(SUBSTRATE_CATEGORIES))
            )
        values[key] = values.get(key, 0) + parse_count(key, count_text)
    return values


def parse_coral_values(raw_values: Sequence[str]) -> Dict[str, int]:
    """Parse and validate --coral pairs against the fixed condition set."""
    values: Dict[str, int] = {}
    for token in split_pairs(raw_values, "--coral"):
        key, count_text = split_pair(
            token, "--coral", "CONDITION=COUNT", "healthy=8,pale=1,bleached=3,dead=0"
        )
        if key not in CORAL_CONDITIONS:
            raise UsageError(
                "unknown coral condition '%s': valid conditions are %s"
                % (key, ", ".join(CORAL_CONDITIONS))
            )
        values[key] = values.get(key, 0) + parse_count(key, count_text)
    return values


def parse_species_values(raw_values: Sequence[str]) -> Dict[str, int]:
    """Parse and validate --species CODE=COUNT pairs."""
    values: Dict[str, int] = {}
    for token in split_pairs(raw_values, "--species"):
        code, count_text = split_pair(token, "--species", "CODE=COUNT", "Acro=6,Poci=4")
        validate_species_code(code)
        values[code] = values.get(code, 0) + parse_count(code, count_text)
    return values


def validate_species_code(code: str) -> None:
    """Species codes are short identifiers that stay readable in a table."""
    if not code:
        raise UsageError("species code must not be empty")
    if len(code) > 32:
        raise UsageError("species code '%s' is too long (max 32 characters)" % code)
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    if set(code) - allowed:
        raise UsageError(
            "invalid species code '%s': use letters, digits, dot, dash or underscore" % code
        )


def parse_metres(raw: Optional[str], flag: str) -> Optional[float]:
    """Validate an optional metres value such as --depth 6."""
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise UsageError(
            "invalid %s '%s': expected a number in metres >= 0 (example: %s 6)" % (flag, raw, flag)
        )
    if value < 0 or value != value or value in (float("inf"), float("-inf")):
        raise UsageError(
            "invalid %s '%s': expected a number in metres >= 0 (example: %s 6)" % (flag, raw, flag)
        )
    return value


def parse_iso_date(raw: Optional[str]) -> str:
    """Validate --date as YYYY-MM-DD, defaulting to today, and name the problem."""
    if raw is None:
        return date.today().isoformat()
    try:
        return date.fromisoformat(str(raw)).isoformat()
    except ValueError:
        raise UsageError(
            "invalid --date '%s': expected YYYY-MM-DD (example: --date 2024-06-01)" % raw
        )


# --------------------------------------------------------------------------- #
# Interactive entry - reached only when stdin is a terminal and no flags were given
# --------------------------------------------------------------------------- #


def read_line(prompt_text: str) -> str:
    """Read one line, treating a closed stream as an empty answer."""
    try:
        return input(prompt_text).strip()
    except EOFError:
        return ""


def collect_substrate_interactive() -> Dict[str, int]:
    """Prompt for substrate pairs one at a time until a blank line."""
    print("Substrate tallies — one CATEGORY=COUNT per line, blank line to finish.")
    print("Categories: %s" % ", ".join(SUBSTRATE_CATEGORIES))
    answers: List[str] = []
    while True:
        answer = read_line("  substrate> ")
        if not answer:
            break
        answers.append(answer)
    return parse_substrate_values(answers) if answers else {}


def collect_coral_interactive() -> Dict[str, int]:
    """Prompt for each coral condition in turn; Enter records zero."""
    print("Coral colony condition counts — press Enter to record 0.")
    values: Dict[str, int] = {}
    for condition in CORAL_CONDITIONS:
        answer = read_line("  %s> " % condition)
        values[condition] = parse_count(condition, answer) if answer else 0
    return values


def collect_species_interactive() -> Dict[str, int]:
    """Prompt for species pairs one at a time until a blank line."""
    print("Species tallies — one CODE=COUNT per line, blank line to finish.")
    answers: List[str] = []
    while True:
        answer = read_line("  species> ")
        if not answer:
            break
        answers.append(answer)
    return parse_species_values(answers) if answers else {}


# --------------------------------------------------------------------------- #
# Shared report pieces
# --------------------------------------------------------------------------- #


def metadata_lines(project: Mapping[str, Any], project_path: str) -> List[str]:
    """The transect header block: site, date, observer, depth, length, quadrats."""
    fields = [
        ("Project", project_path),
        ("Site", str(project.get("site") or "(not recorded)")),
        ("Date", str(project.get("date") or "(not recorded)")),
        ("Observer", str(project.get("observer") or "(not recorded)")),
        ("Depth", format_metres(project.get("depth_m"))),
        ("Transect length", format_metres(project.get("transect_length_m"))),
        ("Quadrat count", str(len(project.get("quadrats") or []))),
    ]
    width = max(len(name) for name, _ in fields)
    return ["  %s : %s" % (name.ljust(width), value) for name, value in fields]


def bleaching_notes_lines(project: Mapping[str, Any]) -> List[str]:
    """Footer states the formula, the denominator rule and the fixed bands."""
    rows = bleaching_rows(project)
    transect = rows[-1]
    mean_percent = mean_quadrat_percent(project)
    return [
        "Bleaching % = bleached / (healthy + pale + bleached) x 100; recently dead colonies",
        "are recorded but excluded from the denominator.",
        "Transect %% uses summed colony counts (%d / %d), not the mean of the quadrat "
        "percentages (%s%%)." % (transect.bleached, transect.live, format_percent(mean_percent)),
        "Bands: none = 0.00%, mild = 0.01-10.00%, moderate = 10.01-30.00%, severe = > 30.00%.",
    ]


def build_report(project: Mapping[str, Any], project_path: str) -> str:
    """Render the whole terminal report: header block plus the three tables."""
    title = "%s transect report" % TOOL_NAME
    lines: List[str] = [title, "=" * len(title), ""]
    lines.extend(metadata_lines(project, project_path))
    lines.append("")

    lines.append("Substrate tallies (per quadrat)")
    lines.append("")
    substrate_rows = [
        [display_name(quadrat)]
        + [str(int((quadrat.get("substrate") or {}).get(cat, 0))) for cat in SUBSTRATE_CATEGORIES]
        + [str(substrate_total(quadrat))]
        for quadrat in project.get("quadrats") or []
    ]
    lines.append(render_table(SUBSTRATE_TABLE_HEADERS, substrate_rows))
    lines.append("")

    lines.append("Coral condition and bleaching")
    lines.append("")
    bleaching_table_rows = [
        [
            row.label,
            row.healthy,
            row.pale,
            row.bleached,
            row.dead,
            row.live,
            format_percent(row.percent),
            row.band,
            row.notes,
        ]
        for row in bleaching_rows(project)
    ]
    lines.append(render_table(CORAL_TABLE_HEADERS, bleaching_table_rows))
    lines.append("")
    lines.extend(bleaching_notes_lines(project))
    lines.append("")

    lines.append("Species abundance")
    lines.append("")
    rows = abundance_rows(project)
    if rows:
        species_rows = [
            [row.display_code, row.total_count, row.quadrats_present, "%.2f" % row.relative_percent]
            for row in rows
        ]
        lines.append(render_table(SPECIES_TABLE_HEADERS, species_rows, left_align=(0,)))
    else:
        lines.append("  %s" % EMPTY_SPECIES_HINT)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CSV export - consumes the same computed rows as the terminal report
# --------------------------------------------------------------------------- #


def write_csv(path: Path, headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> Tuple[str, bool]:
    """Write one CSV atomically; return its full path and whether it existed."""
    existed = path.exists()
    directory = path.parent if str(path.parent) else Path(".")
    handle = None
    tmp_path: Optional[Path] = None
    try:
        descriptor, tmp_name = tempfile.mkstemp(
            prefix=".%s." % path.name, suffix=".tmp", dir=str(directory)
        )
        tmp_path = Path(tmp_name)
        handle = os.fdopen(descriptor, "w", encoding="utf-8", newline="")
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(list(headers))
        for row in rows:
            writer.writerow(list(row))
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        handle = None
        os.replace(str(tmp_path), str(path))
        tmp_path = None
    except OSError as exc:
        raise ProjectFileError("%s: cannot write CSV: %s" % (path, exc.strerror or exc))
    finally:
        if handle is not None:
            with contextlib.suppress(OSError):
                handle.close()
        if tmp_path is not None and tmp_path.exists():
            with contextlib.suppress(OSError):
                tmp_path.unlink()
    return str(path.resolve()), existed


def export_csv(project: Mapping[str, Any], target: Path) -> List[Tuple[str, bool]]:
    """Write quadrats.csv, bleaching.csv and species.csv into target."""
    if target.exists() and not target.is_dir():
        raise UsageError("--csv %s exists and is not a directory" % target)
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ProjectFileError("cannot create %s: %s" % (target, exc.strerror or exc))

    quadrat_rows = []
    for quadrat in project.get("quadrats") or []:
        counts = coral_counts(quadrat)
        live = counts["healthy"] + counts["pale"] + counts["bleached"]
        percent = bleaching_percent(counts["bleached"], live)
        quadrat_rows.append(
            [display_name(quadrat)]
            + [int((quadrat.get("substrate") or {}).get(cat, 0)) for cat in SUBSTRATE_CATEGORIES]
            + [
                substrate_total(quadrat),
                counts["healthy"],
                counts["pale"],
                counts["bleached"],
                counts["dead"],
                live,
                format_percent(percent),
                bleach_band(percent),
                str(quadrat.get("notes") or ""),
            ]
        )

    bleaching_table_rows = [
        [
            row.label,
            row.healthy,
            row.pale,
            row.bleached,
            row.dead,
            row.live,
            format_percent(row.percent),
            row.band,
        ]
        for row in bleaching_rows(project)
    ]

    species_rows = [
        [row.display_code, row.total_count, row.quadrats_present, "%.2f" % row.relative_percent]
        for row in abundance_rows(project)
    ]

    return [
        write_csv(target / "quadrats.csv", CSV_QUADRAT_HEADERS, quadrat_rows),
        write_csv(target / "bleaching.csv", CSV_BLEACHING_HEADERS, bleaching_table_rows),
        write_csv(target / "species.csv", CSV_SPECIES_HEADERS, species_rows),
    ]


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def cmd_init(args: argparse.Namespace) -> int:
    """Create the local JSON project file (refusing to clobber one without --force)."""
    path: Path = args.project
    if path.exists() and not args.force:
        raise UsageError("%s already exists (use --force to overwrite)" % path)
    site = (args.site or "").strip()
    if not site:
        raise UsageError("--site must not be empty")
    observer = (args.observer or "").strip()
    survey_date = parse_iso_date(args.date)
    depth_m = parse_metres(args.depth, "--depth")
    transect_length_m = parse_metres(args.transect_length, "--transect-length")
    project = new_project(site, survey_date, observer, depth_m, transect_length_m)
    save_project(path, project)
    print("created project %s" % path.resolve())
    _print_metadata_block(project)
    print("")
    print(
        'next: reeftally add %s --substrate hard=12,sand=3 '
        "--coral healthy=8,pale=1,bleached=3,dead=0 --species Acro=6" % path
    )
    return EXIT_OK


def _print_metadata_block(project: Mapping[str, Any]) -> None:
    """Print the metadata that was written, aligned in one column."""
    fields = [
        ("site", str(project.get("site") or "(not recorded)")),
        ("date", str(project.get("date") or "(not recorded)")),
        ("observer", str(project.get("observer") or "(not recorded)")),
        ("depth", format_metres(project.get("depth_m"))),
        ("transect length", format_metres(project.get("transect_length_m"))),
        ("quadrats", str(len(project.get("quadrats") or []))),
    ]
    width = max(len(name) for name, _ in fields)
    for name, value in fields:
        print("  %s : %s" % (name.ljust(width), value))


def cmd_add(args: argparse.Namespace) -> int:
    """Append one quadrat, from flags or from interactive prompts."""
    path: Path = args.project
    project = load_project(path)
    substrate = parse_substrate_values(args.substrate)
    coral = parse_coral_values(args.coral)
    species = parse_species_values(args.species)

    if not substrate and not coral and not species:
        if not sys.stdin.isatty():
            raise UsageError(
                "no tallies given and stdin is not a terminal — pass "
                "--substrate hard=12,sand=3 (with optional --coral and --species)"
            )
        substrate = collect_substrate_interactive()
        if not substrate:
            raise UsageError(
                "no substrate tallies recorded — nothing to add "
                "(one pair is enough, e.g. --substrate hard=12)"
            )
        coral = collect_coral_interactive()
        species = collect_species_interactive()

    if not substrate:
        raise UsageError(
            "no substrate tallies recorded — nothing to add "
            "(one pair is enough, e.g. --substrate hard=12)"
        )

    label: Optional[str] = None
    if args.label is not None:
        label = args.label.strip()
        if not label:
            raise UsageError("--label must not be empty when it is given")
        if len(label) > 24:
            raise UsageError("--label '%s' is too long (max 24 characters)" % label)

    numbers = [int(quadrat.get("number", 0)) for quadrat in project["quadrats"]]
    number = (max(numbers) + 1) if numbers else 1
    quadrat = {
        "number": number,
        "label": label,
        "substrate": {key: value for key, value in substrate.items() if value > 0},
        "coral_condition": {key: int(coral.get(key, 0)) for key in CORAL_CONDITIONS},
        "species": {code: value for code, value in species.items() if value > 0},
        "notes": (args.notes.strip() if args.notes else None),
        "recorded_at": utc_now(),
    }
    project["quadrats"].append(quadrat)
    save_project(path, project)

    colonies = coral_total(quadrat)
    print(
        "added %s (substrate %d, %d %s, %d species)"
        % (
            display_name(quadrat),
            substrate_total(quadrat),
            colonies,
            plural(colonies, "coral colony", "coral colonies"),
            len(quadrat["species"]),
        )
    )
    return EXIT_OK


def cmd_report(args: argparse.Namespace) -> int:
    """Print the transect report, optionally exporting CSV alongside it."""
    path: Path = args.project
    project = load_project(path)
    if not project["quadrats"]:
        print(EMPTY_REPORT_HINT.format(path=path))
        return EXIT_OK
    print(build_report(project, str(path)))
    if args.csv is not None:
        written = export_csv(project, Path(args.csv))
        print("")
        for full_path, existed in written:
            print("wrote %s (%s)" % (full_path, "overwrote existing" if existed else "new file"))
    return EXIT_OK


def cmd_remove(args: argparse.Namespace) -> int:
    """Delete the highest-numbered quadrat after confirmation (or with --yes)."""
    path: Path = args.project
    project = load_project(path)
    quadrats = project["quadrats"]
    if not quadrats:
        raise UsageError("no quadrats to remove")
    last = quadrats[-1]
    name = display_name(last)
    colonies = coral_total(last)
    print(
        "about to delete %s (substrate %d, %d %s, %d species)"
        % (
            name,
            substrate_total(last),
            colonies,
            plural(colonies, "coral colony", "coral colonies"),
            len(last.get("species") or {}),
        )
    )
    if not args.yes:
        if not sys.stdin.isatty():
            raise UsageError(
                "refusing to prompt without a terminal — pass --yes to remove %s" % name
            )
        answer = read_line("remove %s? [y/N] " % name).lower()
        if answer not in ("y", "yes"):
            print("nothing removed")
            return EXIT_OK
    quadrats.pop()
    save_project(path, project)
    print("removed %s" % name)
    return EXIT_OK


def cmd_species_list(args: argparse.Namespace) -> int:
    """List every recorded or named species code with its observation count."""
    path: Path = args.project
    project = load_project(path)
    rows = abundance_rows(project)
    counts = {row.code: row for row in rows}
    named = {
        str(code): str(name)
        for code, name in (project.get("species_names") or {}).items()
        if name
    }
    if not counts and not named:
        print(EMPTY_SPECIES_HINT)
        return EXIT_OK
    codes = sorted(set(counts) | set(named), key=lambda code: (-_observed(counts, code), code))
    table_rows = [
        [
            code,
            named.get(code, "unnamed"),
            counts[code].quadrats_present if code in counts else 0,
            _observed(counts, code),
        ]
        for code in codes
    ]
    print("Species codes recorded in %s" % path)
    print("")
    print(render_table(SPECIES_LIST_HEADERS, table_rows, left_align=(0, 1)))
    return EXIT_OK


def _observed(counts: Mapping[str, AbundanceRow], code: str) -> int:
    """Total observations for a code, zero when it was only named, never seen."""
    return counts[code].total_count if code in counts else 0


def cmd_species_set(args: argparse.Namespace) -> int:
    """Attach (or clear) a human-readable display name for a species code."""
    path: Path = args.project
    project = load_project(path)
    assignment = args.assignment
    if "=" not in assignment:
        raise UsageError(
            "invalid assignment '%s': expected CODE=Name "
            "(example: reeftally species set %s Acro=Acropora)" % (assignment, path)
        )
    code, _, name = assignment.partition("=")
    code = code.strip()
    name = name.strip()
    if not code:
        raise UsageError(
            "invalid assignment '%s': expected CODE=Name "
            "(example: reeftally species set %s Acro=Acropora)" % (assignment, path)
        )
    validate_species_code(code)
    names = project.setdefault("species_names", {})
    if name:
        names[code] = name
        save_project(path, project)
        print("set %s -> %s" % (code, name))
    else:
        names.pop(code, None)
        save_project(path, project)
        print("cleared the display name for %s" % code)
    return EXIT_OK


# --------------------------------------------------------------------------- #
# Argument parsing and entry point
# --------------------------------------------------------------------------- #

COMMANDS: Tuple[str, ...] = ("init", "add", "report", "remove", "species")

QUICKSTART = """\
Worked example (copy-paste into any directory):

  reeftally init survey.json --site "Hornsby Reef" --date 2024-06-01 \\
      --observer "R. Okafor" --depth 6 --transect-length 10
  reeftally add survey.json --label q1 --substrate hard=12,sand=3 \\
      --coral healthy=8,pale=1,bleached=3,dead=0 --species Acro=6,Poci=4
  reeftally add survey.json --substrate hard=9,rubble=6 \\
      --coral healthy=2,pale=1,bleached=4,dead=1 --species Acro=3,Monti=2
  reeftally species set survey.json Acro=Acropora
  reeftally report survey.json
  reeftally report survey.json --csv out/

Vocabulary:
  substrate categories : hard, soft, dead, algae, rubble, sand, other
  coral conditions     : healthy, pale, bleached, dead (dead excluded from bleaching %)
  bleaching bands      : none 0%, mild >0-10%, moderate >10-30%, severe >30%

Run 'bash demo.sh' for a non-interactive end-to-end walkthrough.
"""

INIT_EXAMPLES = """\
Examples:
  reeftally init survey.json --site "Hornsby Reef"
  reeftally init survey.json --site "Hornsby Reef" --date 2024-06-01 \\
      --observer "R. Okafor" --depth 6 --transect-length 10
  reeftally init survey.json --site "Hornsby Reef" --force   # overwrite on purpose
"""

ADD_EXAMPLES = """\
Examples:
  reeftally add survey.json --substrate hard=12,sand=3 \\
      --coral healthy=8,pale=1,bleached=3,dead=0 --species Acro=6,Poci=4
  reeftally add survey.json --substrate hard=9,rubble=6 --label "A-07"
  reeftally add survey.json            # prompts for each pair, Enter to skip

Substrate categories: hard, soft, dead, algae, rubble, sand, other
Coral conditions   : healthy, pale, bleached, dead (dead is excluded from the
                     bleaching denominator but still recorded)
"""

REPORT_EXAMPLES = """\
Examples:
  reeftally report survey.json
  reeftally report survey.json --csv out/
"""

REMOVE_EXAMPLES = """\
Examples:
  reeftally remove survey.json            # asks: remove q2? [y/N]
  reeftally remove survey.json --yes      # scripted, no prompt
"""

SPECIES_EXAMPLES = """\
Examples:
  reeftally species list survey.json
  reeftally species set survey.json Acro=Acropora
  reeftally species set survey.json Acro=     # clears the display name
"""


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("project", type=Path, help="path of the JSON project file")


def build_parser() -> argparse.ArgumentParser:
    """Assemble the argparse subcommand surface."""
    parser = argparse.ArgumentParser(
        prog="reeftally",
        description=(
            "ReefTally - coral transect quadrat counter, bleaching percentage calculator "
            "and species abundance log. Standard library only, no network, no install step."
        ),
        epilog=QUICKSTART,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version", action="version", version="%s %s" % (TOOL_NAME, __version__)
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    init_parser = subparsers.add_parser(
        "init",
        help="create the JSON project file for one transect survey",
        description="Create the local JSON project file every other command reads and appends to.",
        epilog=INIT_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common_arguments(init_parser)
    init_parser.add_argument("--site", required=True, help="reef site name, e.g. 'Hornsby Reef'")
    init_parser.add_argument("--date", metavar="YYYY-MM-DD", help="survey date (default: today)")
    init_parser.add_argument("--observer", default="", help="observer name")
    init_parser.add_argument("--depth", metavar="METRES", help="survey depth in metres")
    init_parser.add_argument(
        "--transect-length", dest="transect_length", metavar="METRES", help="transect length in metres"
    )
    init_parser.add_argument(
        "--force", action="store_true", help="overwrite an existing project file"
    )
    init_parser.set_defaults(handler=cmd_init)

    add_parser = subparsers.add_parser(
        "add",
        help="record one quadrat (substrate, coral condition, species counts)",
        description=(
            "Append one quadrat to the project. Omit the tally flags to be prompted "
            "for each pair; with no terminal, the flags are required."
        ),
        epilog=ADD_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common_arguments(add_parser)
    add_parser.add_argument(
        "--substrate",
        action="append",
        default=[],
        metavar="CATEGORY=COUNT[,CATEGORY=COUNT]",
        help="substrate tally pairs, e.g. --substrate hard=12,sand=3",
    )
    add_parser.add_argument(
        "--coral",
        action="append",
        default=[],
        metavar="CONDITION=COUNT[,CONDITION=COUNT]",
        help="colony condition counts, e.g. --coral healthy=8,pale=1,bleached=3,dead=0",
    )
    add_parser.add_argument(
        "--species",
        action="append",
        default=[],
        metavar="CODE=COUNT[,CODE=COUNT]",
        help="species tally pairs, e.g. --species Acro=6,Poci=4",
    )
    add_parser.add_argument("--label", default=None, help="optional quadrat label, e.g. A-07")
    add_parser.add_argument("--notes", default=None, help="free-text note for this quadrat")
    add_parser.set_defaults(handler=cmd_add)

    report_parser = subparsers.add_parser(
        "report",
        help="print the transect tables, optionally exporting CSV",
        description=(
            "Print the transect header block, the per-quadrat substrate and coral tables, "
            "the bleaching summary with its TRANSECT row and the species abundance table."
        ),
        epilog=REPORT_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common_arguments(report_parser)
    report_parser.add_argument(
        "--csv",
        metavar="DIR",
        default=None,
        help="also write quadrats.csv, bleaching.csv and species.csv into DIR",
    )
    report_parser.set_defaults(handler=cmd_report)

    remove_parser = subparsers.add_parser(
        "remove",
        help="delete the last quadrat so a mis-typed entry can be corrected",
        description="Delete the highest-numbered quadrat after confirming; --yes skips the prompt.",
        epilog=REMOVE_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common_arguments(remove_parser)
    remove_parser.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    remove_parser.set_defaults(handler=cmd_remove)

    species_parser = subparsers.add_parser(
        "species",
        help="list species codes or attach display names to them",
        description="Show and edit the display names attached to species codes.",
        epilog=SPECIES_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    species_subparsers = species_parser.add_subparsers(dest="species_action", metavar="<list|set>")
    species_list_parser = species_subparsers.add_parser(
        "list",
        help="list every species code with its name and observation count",
        epilog=SPECIES_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common_arguments(species_list_parser)
    species_list_parser.set_defaults(handler=cmd_species_list)
    species_set_parser = species_subparsers.add_parser(
        "set",
        help="attach a display name to a species code (CODE=Name)",
        epilog=SPECIES_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common_arguments(species_set_parser)
    species_set_parser.add_argument("assignment", metavar="CODE=Name", help="e.g. Acro=Acropora")
    species_set_parser.set_defaults(handler=cmd_species_set)
    return parser


def _first_command_token(arguments: Sequence[str]) -> Optional[str]:
    """Return the first non-flag token, which must be a known subcommand."""
    for token in arguments:
        if not token.startswith("-"):
            return token
    return None


def configure_output() -> None:
    """Best-effort UTF-8 stdout/stderr so the em dashes survive any console."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            return


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point: dispatch the subcommand and translate failures to exit codes."""
    configure_output()
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if not arguments:
        parser.print_help(sys.stdout)
        print("")
        print(QUICKSTART)
        return EXIT_OK
    candidate = _first_command_token(arguments)
    if candidate is not None and candidate not in COMMANDS:
        print("unknown command: %s" % candidate, file=sys.stderr)
        print("run 'reeftally --help' to see the available commands", file=sys.stderr)
        return EXIT_USAGE
    args = parser.parse_args(arguments)
    try:
        return args.handler(args)
    except ReefTallyError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("error: interrupted — the project file was not modified", file=sys.stderr)
        return EXIT_INTERNAL
    except Exception as exc:  # final safety net: never show a traceback to the user
        print("internal error: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return EXIT_INTERNAL


if __name__ == "__main__":
    sys.exit(main())
