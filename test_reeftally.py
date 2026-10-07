"""Developer smoke suite for ReefTally.

Run either way (both are standard-library only for the tool itself):

    python3 -m unittest test_reeftally
    pytest -q

The suite covers the bleaching formula, the transect rollup, the zero-live-colony
guard, KEY=COUNT parsing, project file round-trip and error handling, the table
renderer, CSV export, the remove and species commands and the CLI exit codes.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

import reeftally

HERE = Path(__file__).resolve().parent
CLI = HERE / "reeftally.py"
ANSI = "\x1b["


def run_cli(*args, cwd=None, stdin_text=""):
    """Run the tool in a subprocess; stdin is a pipe, so prompts are refused."""
    return subprocess.run(
        [sys.executable, str(CLI)] + [str(arg) for arg in args],
        capture_output=True,
        text=True,
        cwd=cwd,
        input=stdin_text,
    )


def quadrat(number, healthy=0, pale=0, bleached=0, dead=0, substrate=None, species=None):
    """Build a quadrat mapping the way the tool stores one."""
    return {
        "number": number,
        "label": None,
        "substrate": substrate or {"hard": 1},
        "coral_condition": {
            "healthy": healthy,
            "pale": pale,
            "bleached": bleached,
            "dead": dead,
        },
        "species": species or {},
        "notes": None,
        "recorded_at": "2024-06-01T09:00:00Z",
    }


class BaseProjectTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "survey.json"

    def make_project(self, quadrats=None, site="Hornsby Reef"):
        project = reeftally.new_project(site, "2024-06-01", "R. Okafor", 6.0, 10.0)
        project["quadrats"] = list(quadrats or [])
        reeftally.save_project(self.path, project)
        return project


class BleachingMetricTests(BaseProjectTest):
    def test_quadrat_percentage_excludes_dead_colonies(self):
        project = self.make_project([quadrat(1, healthy=10, pale=2, bleached=4, dead=3)])
        row = reeftally.bleaching_rows(project)[0]
        self.assertEqual(row.live, 16)
        self.assertEqual(row.dead, 3)
        self.assertEqual(row.percent, 25.0)
        self.assertEqual(reeftally.format_percent(row.percent), "25.00")
        self.assertEqual(row.band, "moderate")

    def test_transect_percentage_uses_summed_counts_not_the_mean(self):
        project = self.make_project(
            [
                quadrat(1, healthy=10, pale=2, bleached=4, dead=3),
                quadrat(2, healthy=1, pale=0, bleached=1, dead=0),
            ]
        )
        rows = reeftally.bleaching_rows(project)
        transect = rows[-1]
        self.assertEqual(transect.scope, "transect")
        self.assertEqual(transect.label, "TRANSECT")
        self.assertEqual((transect.bleached, transect.live), (5, 18))
        self.assertEqual(transect.percent, 27.78)
        self.assertNotEqual(transect.percent, 37.5)  # mean of 25.00 and 50.00
        self.assertEqual(reeftally.mean_quadrat_percent(project), 37.5)

    def test_zero_live_colonies_reads_na_without_dividing_by_zero(self):
        project = self.make_project([quadrat(1, healthy=0, pale=0, bleached=0, dead=5)])
        row = reeftally.bleaching_rows(project)[0]
        self.assertEqual(row.live, 0)
        self.assertIsNone(row.percent)
        self.assertEqual(row.band, "n/a")
        self.assertEqual(reeftally.format_percent(row.percent), "n/a")
        rendered = reeftally.build_report(project, str(self.path))
        self.assertIn("n/a", rendered)

    def test_severity_bands_are_fixed(self):
        cases = [(None, "n/a"), (0.0, "none"), (0.01, "mild"), (10.0, "mild"),
                 (10.01, "moderate"), (30.0, "moderate"), (30.01, "severe"), (100.0, "severe")]
        for percent, expected in cases:
            self.assertEqual(reeftally.bleach_band(percent), expected, percent)

    def test_species_rollup_sorted_by_total_then_code(self):
        project = self.make_project(
            [
                quadrat(1, species={"Acro": 6, "Poci": 4, "Monti": 2}),
                quadrat(2, species={"Acro": 3, "Poci": 4}),
            ]
        )
        rows = reeftally.abundance_rows(project)
        self.assertEqual([r.code for r in rows], ["Acro", "Poci", "Monti"])
        self.assertEqual([r.total_count for r in rows], [9, 8, 2])
        self.assertEqual([r.quadrats_present for r in rows], [2, 2, 1])
        self.assertEqual(rows[0].relative_percent, 47.37)
        self.assertEqual(rows[-1].display_code, "Monti (unnamed)")

    def test_species_rollup_breaks_ties_alphabetically(self):
        project = self.make_project([quadrat(1, species={"Zeta": 3, "Acro": 3, "Mira": 3})])
        self.assertEqual([r.code for r in reeftally.abundance_rows(project)], ["Acro", "Mira", "Zeta"])

    def test_named_code_renders_code_and_name(self):
        project = self.make_project([quadrat(1, species={"Acro": 6})])
        project["species_names"] = {"Acro": "Acropora"}
        row = reeftally.abundance_rows(project)[0]
        self.assertEqual(row.display_code, "Acro (Acropora)")
        self.assertIn("Acro (Acropora)", reeftally.build_report(project, str(self.path)))


class PairParsingTests(unittest.TestCase):
    def test_substrate_pairs_parse_and_accumulate(self):
        values = reeftally.parse_substrate_values(["hard=12,sand=3", "hard=1"])
        self.assertEqual(values, {"hard": 13, "sand": 3})

    def test_negative_count_is_quoted_and_the_rule_restated(self):
        with self.assertRaises(reeftally.UsageError) as caught:
            reeftally.parse_substrate_values(["hard=-3"])
        self.assertEqual(
            str(caught.exception),
            "invalid count for 'hard': '-3' (counts must be whole numbers >= 0)",
        )

    def test_non_integer_count_is_rejected(self):
        with self.assertRaises(reeftally.UsageError) as caught:
            reeftally.parse_substrate_values(["hard=twelve"])
        self.assertIn("invalid count for 'hard': 'twelve'", str(caught.exception))

    def test_unknown_substrate_category_lists_valid_keys(self):
        with self.assertRaises(reeftally.UsageError) as caught:
            reeftally.parse_substrate_values(["hrad=3"])
        self.assertIn("unknown substrate category 'hrad'", str(caught.exception))
        self.assertIn("hard, soft, dead, algae, rubble, sand, other", str(caught.exception))

    def test_malformed_pair_shows_expected_syntax(self):
        with self.assertRaises(reeftally.UsageError) as caught:
            reeftally.parse_substrate_values(["hard"])
        self.assertIn("expected CATEGORY=COUNT", str(caught.exception))
        self.assertIn("--substrate hard=12,sand=3", str(caught.exception))

    def test_species_pair_without_equals_shows_expected_syntax(self):
        with self.assertRaises(reeftally.UsageError) as caught:
            reeftally.parse_species_values(["Acro"])
        self.assertIn("expected CODE=COUNT", str(caught.exception))
        self.assertIn("--species Acro=6,Poci=4", str(caught.exception))

    def test_unknown_coral_condition_is_rejected(self):
        with self.assertRaises(reeftally.UsageError) as caught:
            reeftally.parse_coral_values(["bleeched=3"])
        self.assertIn("healthy, pale, bleached, dead", str(caught.exception))

    def test_bad_species_code_is_rejected(self):
        with self.assertRaises(reeftally.UsageError):
            reeftally.parse_species_values(["Acro pora=3"])


class ProjectFileTests(BaseProjectTest):
    def test_round_trip_preserves_every_field(self):
        project = reeftally.new_project("Hornsby Reef", "2024-06-01", "R. Okafor", 6.0, 10.0)
        project["quadrats"].append(quadrat(1, healthy=8, pale=1, bleached=3, species={"Acro": 6}))
        reeftally.save_project(self.path, project)
        loaded = reeftally.load_project(self.path)
        self.assertEqual(loaded["schema_version"], reeftally.SCHEMA_VERSION)
        self.assertEqual(loaded["site"], "Hornsby Reef")
        self.assertEqual(loaded["quadrats"][0]["coral_condition"]["bleached"], 3)
        self.assertEqual(loaded["quadrats"][0]["species"], {"Acro": 6})
        self.assertIn("created_at", loaded)
        self.assertIn("updated_at", loaded)

    def test_save_is_atomic_and_leaves_no_temp_files(self):
        self.make_project([quadrat(1)])
        leftovers = [p.name for p in self.dir.iterdir() if p.name != self.path.name]
        self.assertEqual(leftovers, [])
        self.assertIn("updated_at", json.loads(self.path.read_text(encoding="utf-8")))

    def test_missing_file_names_the_path_and_the_init_command(self):
        missing = self.dir / "nope.json"
        with self.assertRaises(reeftally.ProjectFileError) as caught:
            reeftally.load_project(missing)
        message = str(caught.exception)
        self.assertIn(str(missing), message)
        self.assertIn("reeftally init", message)

    def test_corrupt_file_reports_path_and_parse_position(self):
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(reeftally.ProjectFileError) as caught:
            reeftally.load_project(self.path)
        message = str(caught.exception)
        self.assertIn(str(self.path), message)
        self.assertIn("line 1 column 2", message)

    def test_newer_schema_version_is_refused(self):
        self.make_project([quadrat(1)])
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data["schema_version"] = 2
        self.path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(reeftally.ProjectFileError) as caught:
            reeftally.load_project(self.path)
        self.assertIn("schema_version 2 is newer than this tool (1)", str(caught.exception))

    def test_quadrats_must_be_a_list(self):
        self.make_project()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data["quadrats"] = {"q1": {}}
        self.path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(reeftally.ProjectFileError):
            reeftally.load_project(self.path)


class RendererTests(unittest.TestCase):
    def test_columns_are_padded_to_the_widest_cell(self):
        table = reeftally.render_table(["Code", "Total"], [["Acro", 12], ["A very long code", 3]])
        lines = table.splitlines()
        self.assertEqual(lines[0], "Code".ljust(16) + "  " + "Total".rjust(5))
        self.assertEqual(lines[1], "-" * 23)
        self.assertEqual(lines[2], "Acro".ljust(16) + "  " + "12".rjust(5))
        self.assertEqual(lines[3], "A very long code".ljust(16) + "  " + "3".rjust(5))
        self.assertNotIn(ANSI, table)

    def test_utf8_names_stay_aligned_when_written_and_read_back(self):
        table = reeftally.render_table(
            ["Code", "Total"],
            [["Acropora cf. hyacinthus", 9], ["Pocillopora damicornis", 4]],
        )
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "table.txt"
            target.write_text(table + "\n", encoding="utf-8")
            self.assertEqual(target.read_text(encoding="utf-8"), table + "\n")
        lines = table.splitlines()
        self.assertTrue(lines[2].startswith("Acropora cf. hyacinthus"))
        self.assertTrue(lines[3].startswith("Pocillopora damicornis"))
        self.assertEqual(lines[2].index("9"), lines[3].index("4"))

    def test_long_values_are_not_truncated(self):
        long_code = "A" * 40
        table = reeftally.render_table(["Code"], [[long_code]])
        self.assertIn(long_code, table)


class CliTests(BaseProjectTest):
    def test_help_lists_every_subcommand_and_exits_zero(self):
        result = run_cli("--help")
        self.assertEqual(result.returncode, 0)
        for command in ("init", "add", "report", "remove", "species"):
            self.assertIn(command, result.stdout)
        self.assertIn("report survey.json --csv out/", result.stdout)

    def test_every_subcommand_help_shows_a_worked_example(self):
        for command in ("init", "add", "report", "remove", "species"):
            result = run_cli(command, "--help")
            self.assertEqual(result.returncode, 0, command)
            self.assertIn("reeftally %s" % command, result.stdout)

    def test_version_flag(self):
        result = run_cli("--version")
        self.assertEqual(result.returncode, 0)
        self.assertIn("ReefTally", result.stdout)

    def test_unknown_command_exits_2_with_message(self):
        result = run_cli("frobnicate")
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown command: frobnicate", result.stderr)

    def test_init_add_report_end_to_end(self):
        init = run_cli(
            "init", self.path, "--site", "Hornsby Reef", "--date", "2024-06-01",
            "--observer", "R. Okafor", "--depth", "6", "--transect-length", "10",
        )
        self.assertEqual(init.returncode, 0, init.stderr)
        self.assertIn(str(self.path), init.stdout)
        written = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(written["quadrats"], [])
        self.assertEqual(written["schema_version"], 1)
        self.assertEqual(written["depth_m"], 6.0)

        add = run_cli(
            "add", self.path, "--substrate", "hard=12,sand=3",
            "--coral", "healthy=8,pale=1,bleached=3,dead=0", "--species", "Acro=6,Poci=4",
        )
        self.assertEqual(add.returncode, 0, add.stderr)
        self.assertIn("added q1 (substrate 15, 12 coral colonies, 2 species)", add.stdout)

        add2 = run_cli("add", self.path, "--substrate", "hard=9,rubble=6", "--coral", "healthy=2,bleached=1")
        self.assertEqual(add2.returncode, 0, add2.stderr)
        self.assertIn("added q2", add2.stdout)

        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual([q["number"] for q in stored["quadrats"]], [1, 2])
        self.assertEqual(stored["quadrats"][0]["substrate"], {"hard": 12, "sand": 3})
        report = run_cli("report", self.path)
        self.assertEqual(report.returncode, 0, report.stderr)
        self.assertIn("Hornsby Reef", report.stdout)
        self.assertIn("TRANSECT", report.stdout)
        self.assertIn("Species abundance", report.stdout)
        self.assertNotIn(ANSI, report.stdout)

    def test_init_refuses_to_overwrite_without_force(self):
        self.make_project()
        before = self.path.read_bytes()
        result = run_cli("init", self.path, "--site", "Other Reef")
        self.assertEqual(result.returncode, 2)
        self.assertIn("already exists (use --force to overwrite)", result.stderr)
        self.assertEqual(self.path.read_bytes(), before)

    def test_init_force_overwrites(self):
        self.make_project([quadrat(1)])
        result = run_cli("init", self.path, "--site", "Other Reef", "--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["quadrats"], [])

    def test_invalid_add_leaves_the_project_file_byte_identical(self):
        self.make_project([quadrat(1, healthy=4, bleached=1)])
        before = self.path.read_bytes()
        result = run_cli("add", self.path, "--substrate", "hard=-3")
        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid count for 'hard': '-3'", result.stderr)
        self.assertEqual(self.path.read_bytes(), before)

        typo = run_cli("add", self.path, "--substrate", "hrad=3")
        self.assertEqual(typo.returncode, 2)
        self.assertIn("hard, soft, dead, algae, rubble, sand, other", typo.stderr)

        no_equals = run_cli("add", self.path, "--species", "Acro")
        self.assertEqual(no_equals.returncode, 2)
        self.assertIn("expected CODE=COUNT", no_equals.stderr)
        self.assertEqual(self.path.read_bytes(), before)

    def test_add_without_flags_and_without_a_terminal_explains_the_flags(self):
        self.make_project()
        result = run_cli("add", self.path)
        self.assertEqual(result.returncode, 2)
        self.assertIn("stdin is not a terminal", result.stderr)

    def test_report_on_an_empty_project_invites_the_next_command(self):
        self.make_project()
        result = run_cli("report", self.path)
        self.assertEqual(result.returncode, 0)
        self.assertIn("no quadrats recorded yet", result.stdout)
        self.assertIn("reeftally add", result.stdout)

    def test_corrupt_project_exits_non_zero_without_a_traceback(self):
        self.path.write_text("{not json", encoding="utf-8")
        result = run_cli("report", self.path)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(str(self.path), result.stderr)
        self.assertIn("line 1 column 2", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_newer_schema_is_refused_by_every_command(self):
        self.make_project([quadrat(1)])
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data["schema_version"] = 2
        self.path.write_text(json.dumps(data), encoding="utf-8")
        before = self.path.read_bytes()
        for args in (["report", self.path], ["species", "list", self.path], ["add", self.path, "--substrate", "hard=1"]):
            result = run_cli(*args)
            self.assertEqual(result.returncode, 2, args)
            self.assertIn("schema_version 2 is newer than this tool (1)", result.stderr)
        self.assertEqual(self.path.read_bytes(), before)


class CsvExportTests(BaseProjectTest):
    def setUp(self):
        super().setUp()
        self.make_project(
            [
                quadrat(1, healthy=8, pale=1, bleached=3, dead=0, substrate={"hard": 12, "sand": 3}, species={"Acro": 6, "Poci": 4}),
                quadrat(2, healthy=2, pale=1, bleached=4, dead=1, substrate={"hard": 9, "rubble": 6}, species={"Acro": 3}),
            ]
        )
        reeftally_project = reeftally.load_project(self.path)
        reeftally_project["species_names"] = {"Acro": "Acropora"}
        reeftally.save_project(self.path, reeftally_project)

    def test_export_creates_the_directory_and_prints_full_paths(self):
        target = self.dir / "nested" / "out"
        result = run_cli("report", self.path, "--csv", target)
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ("quadrats.csv", "bleaching.csv", "species.csv"):
            self.assertTrue((target / name).exists(), name)
            self.assertIn(str((target / name).resolve()), result.stdout)
        self.assertIn("new file", result.stdout)

    def test_rerun_reports_that_existing_files_were_overwritten(self):
        result = run_cli("report", self.path, "--csv", self.dir / "out")
        self.assertEqual(result.returncode, 0, result.stderr)
        again = run_cli("report", self.path, "--csv", self.dir / "out")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("overwrote existing", again.stdout)

    def test_bleaching_csv_has_a_transect_row_and_two_decimal_percentages(self):
        run_cli("report", self.path, "--csv", self.dir / "out")
        lines = (self.dir / "out" / "bleaching.csv").read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], "Quadrat,healthy,pale,bleached,dead coral,live,bleached %,band")
        self.assertEqual(len(lines), 4)  # header + q1 + q2 + TRANSECT
        self.assertTrue(lines[-1].startswith("TRANSECT,"))
        self.assertIn(",25.00,", lines[1])
        self.assertIn(",57.14,", lines[2])
        self.assertIn(",36.84,", lines[3])

    def test_quadrats_csv_headers_match_the_terminal_column_names(self):
        run_cli("report", self.path, "--csv", self.dir / "out")
        header = (self.dir / "out" / "quadrats.csv").read_text(encoding="utf-8").splitlines()[0]
        self.assertEqual(header.split(","), list(reeftally.CSV_QUADRAT_HEADERS))
        report = run_cli("report", self.path).stdout
        for name in ("Quadrat", "hard", "dead coral", "bleached %", "band"):
            self.assertIn(name, report)

    def test_species_csv_uses_the_display_name(self):
        run_cli("report", self.path, "--csv", self.dir / "out")
        body = (self.dir / "out" / "species.csv").read_text(encoding="utf-8")
        self.assertIn("Acro (Acropora),9,2,", body)
        self.assertIn("(unnamed)", body)


class RemoveAndSpeciesTests(BaseProjectTest):
    def test_remove_yes_deletes_only_the_last_quadrat(self):
        self.make_project([quadrat(1, healthy=4, species={"Acro": 2}), quadrat(2, healthy=2)])
        result = run_cli("remove", self.path, "--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("removed q2", result.stdout)
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(len(stored["quadrats"]), 1)
        self.assertEqual(stored["quadrats"][0]["coral_condition"]["healthy"], 4)
        self.assertEqual(stored["quadrats"][0]["species"], {"Acro": 2})

    def test_remove_on_an_empty_project_exits_2(self):
        self.make_project()
        result = run_cli("remove", self.path)
        self.assertEqual(result.returncode, 2)
        self.assertIn("no quadrats to remove", result.stderr)

    def test_remove_without_a_terminal_refuses_to_prompt(self):
        self.make_project([quadrat(1)])
        before = self.path.read_bytes()
        result = run_cli("remove", self.path)
        self.assertEqual(result.returncode, 2)
        self.assertIn("pass --yes", result.stderr)
        self.assertEqual(self.path.read_bytes(), before)

    def test_declining_the_prompt_leaves_the_file_byte_identical(self):
        self.make_project([quadrat(1), quadrat(2)])
        before = self.path.read_bytes()
        namespace = reeftally.build_parser().parse_args(["remove", str(self.path)])
        with mock.patch("sys.stdin.isatty", return_value=True), \
                mock.patch("builtins.input", return_value="n") as prompt, \
                redirect_stdout(StringIO()):
            code = reeftally.cmd_remove(namespace)
        self.assertEqual(code, 0)
        self.assertIn("remove q2? [y/N]", prompt.call_args[0][0])
        self.assertEqual(self.path.read_bytes(), before)

    def test_confirming_the_prompt_deletes_the_last_quadrat(self):
        self.make_project([quadrat(1), quadrat(2)])
        namespace = reeftally.build_parser().parse_args(["remove", str(self.path)])
        with mock.patch("sys.stdin.isatty", return_value=True), \
                mock.patch("builtins.input", return_value="y"), \
                redirect_stdout(StringIO()):
            code = reeftally.cmd_remove(namespace)
        self.assertEqual(code, 0)
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual([q["number"] for q in stored["quadrats"]], [1])

    def test_species_set_then_report_and_csv_show_code_and_name(self):
        self.make_project([quadrat(1, species={"Acro": 6, "Poci": 4})])
        result = run_cli("species", "set", self.path, "Acro=Acropora")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf-8"))["species_names"], {"Acro": "Acropora"}
        )
        report = run_cli("report", self.path).stdout
        self.assertIn("Acro (Acropora)", report)
        self.assertIn("Poci (unnamed)", report)
        export = run_cli("report", self.path, "--csv", self.dir / "out")
        self.assertEqual(export.returncode, 0, export.stderr)
        self.assertIn("Acro (Acropora)", (self.dir / "out" / "species.csv").read_text(encoding="utf-8"))

    def test_species_list_shows_name_quadrats_and_observations(self):
        self.make_project([quadrat(1, species={"Acro": 6, "Poci": 4}), quadrat(2, species={"Acro": 3})])
        run_cli("species", "set", self.path, "Acro=Acropora")
        result = run_cli("species", "list", self.path)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [line for line in result.stdout.splitlines() if line.startswith("Acro")]
        self.assertTrue(lines[0].startswith("Acro"))
        self.assertIn("Acropora", lines[0])
        self.assertIn("unnamed", result.stdout)
        self.assertIn("Observations", result.stdout)

    def test_species_list_on_an_empty_project_invites_the_next_command(self):
        self.make_project()
        result = run_cli("species", "list", self.path)
        self.assertEqual(result.returncode, 0)
        self.assertIn("no species recorded yet", result.stdout)

    def test_species_set_without_equals_shows_the_expected_syntax(self):
        self.make_project([quadrat(1, species={"Acro": 1})])
        before = self.path.read_bytes()
        result = run_cli("species", "set", self.path, "Acropora")
        self.assertEqual(result.returncode, 2)
        self.assertIn("expected CODE=Name", result.stderr)
        self.assertIn("Acro=Acropora", result.stderr)
        self.assertEqual(self.path.read_bytes(), before)

    def test_species_set_with_an_empty_name_clears_the_mapping(self):
        self.make_project([quadrat(1, species={"Acro": 1})])
        run_cli("species", "set", self.path, "Acro=Acropora")
        result = run_cli("species", "set", self.path, "Acro=")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["species_names"], {})


class InteractiveAddTests(BaseProjectTest):
    def test_interactive_add_saves_one_quadrat_from_typed_pairs(self):
        self.make_project()
        namespace = reeftally.build_parser().parse_args(["add", str(self.path)])
        answers = ["hard=12", "sand=3", "", "8", "1", "3", "0", "Acro=6", ""]
        out = StringIO()
        with mock.patch("sys.stdin.isatty", return_value=True), \
                mock.patch("builtins.input", side_effect=answers), \
                redirect_stdout(out):
            code = reeftally.cmd_add(namespace)
        self.assertEqual(code, 0)
        self.assertIn("added q1 (substrate 15, 12 coral colonies, 1 species)", out.getvalue())
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        quadrat_stored = stored["quadrats"][0]
        self.assertEqual(quadrat_stored["substrate"], {"hard": 12, "sand": 3})
        self.assertEqual(quadrat_stored["coral_condition"], {"healthy": 8, "pale": 1, "bleached": 3, "dead": 0})
        self.assertEqual(quadrat_stored["species"], {"Acro": 6})

    def test_interactive_add_rejects_a_bad_pair_and_keeps_the_file_untouched(self):
        self.make_project()
        before = self.path.read_bytes()
        namespace = reeftally.build_parser().parse_args(["add", str(self.path)])
        with mock.patch("sys.stdin.isatty", return_value=True), \
                mock.patch("builtins.input", side_effect=["hrad=3", ""]), \
                redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            with self.assertRaises(reeftally.UsageError):
                reeftally.cmd_add(namespace)
        self.assertEqual(self.path.read_bytes(), before)

    def test_interactive_add_with_no_substrate_pairs_errors(self):
        self.make_project()
        namespace = reeftally.build_parser().parse_args(["add", str(self.path)])
        with mock.patch("sys.stdin.isatty", return_value=True), \
                mock.patch("builtins.input", side_effect=[""]), \
                redirect_stdout(StringIO()):
            with self.assertRaises(reeftally.UsageError):
                reeftally.cmd_add(namespace)


class FilterAndLabelTests(BaseProjectTest):
    def test_label_overrides_the_quadrat_name_in_tables_and_bare_add_uses_the_number(self):
        self.make_project()
        first = reeftally.build_parser().parse_args(
            ["add", str(self.path), "--substrate", "hard=4", "--label", "A-07"]
        )
        with redirect_stdout(StringIO()):
            reeftally.cmd_add(first)
        second = reeftally.build_parser().parse_args(["add", str(self.path), "--substrate", "hard=5"])
        with redirect_stdout(StringIO()):
            reeftally.cmd_add(second)
        report = reeftally.build_report(reeftally.load_project(self.path), str(self.path))
        self.assertIn("A-07", report)
        self.assertIn("q2", report)
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["quadrats"][0]["label"], "A-07")
        self.assertIsNone(stored["quadrats"][1]["label"])

    def test_notes_are_stored_and_shown(self):
        self.make_project()
        namespace = reeftally.build_parser().parse_args(
            ["add", str(self.path), "--substrate", "hard=4", "--notes", "turbo snail scar"]
        )
        with redirect_stdout(StringIO()):
            reeftally.cmd_add(namespace)
        report = reeftally.build_report(reeftally.load_project(self.path), str(self.path))
        self.assertIn("turbo snail scar", report)

    def test_zero_counts_are_not_stored_as_observations(self):
        self.make_project()
        namespace = reeftally.build_parser().parse_args(
            ["add", str(self.path), "--substrate", "hard=5,soft=0", "--species", "Acro=0"]
        )
        with redirect_stdout(StringIO()):
            reeftally.cmd_add(namespace)
        stored = json.loads(self.path.read_text(encoding="utf-8"))["quadrats"][0]
        self.assertEqual(stored["substrate"], {"hard": 5})
        self.assertEqual(stored["species"], {})
        self.assertEqual(reeftally.abundance_rows({"quadrats": [stored], "species_names": {}}), [])

    def test_reports_are_deterministic_for_the_same_project(self):
        self.make_project(
            [
                quadrat(1, healthy=8, pale=1, bleached=3, dead=0, substrate={"hard": 12, "sand": 3}, species={"Acro": 6, "Poci": 4}),
                quadrat(2, healthy=2, pale=1, bleached=4, dead=1, substrate={"hard": 9, "rubble": 6}, species={"Acro": 3, "Monti": 2}),
            ]
        )
        project = reeftally.load_project(self.path)
        first = reeftally.build_report(project, str(self.path))
        second = reeftally.build_report(reeftally.load_project(self.path), str(self.path))
        self.assertEqual(first, second)
        self.assertNotIn(ANSI, first)
        self.assertIn("Bands: none = 0.00%, mild = 0.01-10.00%, moderate = 10.01-30.00%, severe = > 30.00%.", first)


class MainEntryTests(BaseProjectTest):
    def test_bare_invocation_prints_help_and_the_quickstart(self):
        out = StringIO()
        with redirect_stdout(out):
            code = reeftally.main([])
        self.assertEqual(code, 0)
        self.assertIn("<command>", out.getvalue())
        self.assertIn("reeftally report survey.json --csv out/", out.getvalue())

    def test_main_returns_two_and_writes_to_stderr_for_a_bad_command(self):
        err = StringIO()
        with redirect_stderr(err):
            code = reeftally.main(["frobnicate"])
        self.assertEqual(code, 2)
        self.assertIn("unknown command: frobnicate", err.getvalue())

    def test_main_maps_usage_errors_to_exit_code_two(self):
        err = StringIO()
        with redirect_stderr(err):
            code = reeftally.main(["report", str(self.dir / "missing.json")])
        self.assertEqual(code, 2)
        self.assertIn("project file not found", err.getvalue())


if __name__ == "__main__":
    unittest.main()
