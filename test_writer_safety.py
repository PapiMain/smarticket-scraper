"""Offline tests of the actual writer AST; no scraper imports or live state."""
import ast
import contextlib
import io
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest

from papi_sync_guard import (MISSING, RunReport, normalize_title,
                             sold_from_capacity, titles_match, verify_appsheet_response)


class Response:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class WriterImportContract(unittest.TestCase):
    def test_guard_dependencies_imported_under_actual_names(self):
        tree = ast.parse(Path(__file__).with_name("run_scraper.py").read_text(encoding="utf-8"))
        imports = [alias for node in tree.body
                   if isinstance(node, ast.ImportFrom)
                   and node.module == "papi_sync_guard" and node.level == 0
                   for alias in node.names]
        required = {"MISSING", "RunReport", "VolumeGuard", "titles_match",
                    "normalize_title", "sold_from_capacity", "verify_appsheet_response"}
        for name in sorted(required):
            with self.subTest(name=name):
                self.assertTrue(
                    any(alias.name == name and alias.asname in (None, name)
                        for alias in imports),
                    f"papi_sync_guard must supply {name} under its actual name",
                )


class WriterSafety(unittest.TestCase):
    def setUp(self):
        self.posts = []
        self.successes = []
        self.rows = [{"ID": "one", "הפקה": "Aladdin", "תאריך": "2026-10-20",
                      "ארגון": "סמארטיקט", "קיבלו": 100}]
        self.payload = {"Rows": [{"ID": "one"}]}
        successes = self.successes

        class Guard:
            def __init__(self, *args, **kwargs):
                pass

            def check(self, count):
                return True, "synthetic guard"

            def record_success(self, count):
                successes.append(count)

        def post(*args, **kwargs):
            self.posts.append(kwargs["json"]["Rows"])
            return Response(self.payload)

        self.scope = dict(
            os=SimpleNamespace(environ={"APPSHEET_APP_ID": "dummy", "APPSHEET_APP_KEY": "dummy"}),
            VolumeGuard=Guard, RunReport=RunReport, datetime=datetime,
            pytz=SimpleNamespace(timezone=lambda _: timezone.utc),
            get_appsheet_data=lambda _: self.rows, requests=SimpleNamespace(post=post),
            DRY_RUN=False, MISSING=MISSING, sold_from_capacity=sold_from_capacity,
            normalize_title=normalize_title, titles_match=titles_match,
            verify_appsheet_response=verify_appsheet_response,
        )
        tree = ast.parse(Path(__file__).with_name("run_scraper.py").read_text(encoding="utf-8"))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name == "update_appsheet_batch")
        exec(compile(ast.Module(body=[node], type_ignores=[]), "run_scraper.py", "exec"), self.scope)

    def show(self, **changes):
        show = dict(name="Aladdin", searched_name="Aladdin", date="20/10/2026",
                    time="18:00", hall="Hall", site_tag="Papi", available_seats=30,
                    url="https://synthetic.invalid/card/1")
        show.update(changes)
        return show

    def run_writer(self, shows):
        with contextlib.redirect_stdout(io.StringIO()):
            return self.scope["update_appsheet_batch"](shows)

    def assert_rejected(self, shows):
        report = self.run_writer(shows)
        self.assertFalse(report.ok)
        self.assertEqual(report.matched, 0)
        self.assertEqual(self.posts, [])
        self.assertEqual(self.successes, [])
        return report

    def test_wrong_unique_substring(self):
        self.rows[0]["הפקה"] = "Aladdin festival"
        self.assert_rejected([self.show()])

    def test_scraped_title_not_search_term_controls_match(self):
        self.assert_rejected([self.show(name="Aladdin festival", searched_name="Aladdin")])

    def test_empty_production(self):
        for name in (None, "", "   "):
            with self.subTest(name=name):
                self.rows[0]["הפקה"] = name
                self.assert_rejected([self.show()])

    def test_normalized_exact_match(self):
        report = self.run_writer([self.show(name="  ALADDIN!  ")])
        self.assertTrue(report.ok)
        self.assertEqual((report.matched, report.confirmed), (1, 1))
        self.assertEqual(self.posts[0][0]["נמכרו"], 70)
        self.assertEqual(self.successes, [1])

    def test_date_and_org_required(self):
        for changes in (dict(date="21/10/2026"), dict(site_tag="Friends")):
            with self.subTest(changes=changes):
                self.assert_rejected([self.show(**changes)])

    def test_multiple_matching_rows_are_ambiguous(self):
        self.rows.append(dict(self.rows[0], ID="two"))
        report = self.assert_rejected([self.show()])
        self.assertEqual(len(report.ambiguous), 1)

    def test_missing_seats_and_capacity(self):
        self.assert_rejected([self.show(available_seats=None)])
        self.rows[0]["קיבלו"] = None
        self.assert_rejected([self.show()])

    def test_dry_run(self):
        self.scope["DRY_RUN"] = True
        report = self.run_writer([self.show()])
        self.assertTrue(report.ok)
        self.assertEqual((report.matched, report.confirmed), (1, 0))
        self.assertEqual(self.posts, [])
        self.assertEqual(self.successes, [])

    def test_invalid_response_never_records_success(self):
        self.payload = {"Rows": "x"}
        report = self.run_writer([self.show()])
        self.assertFalse(report.ok)
        self.assertEqual(report.confirmed, 0)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.successes, [])

    def test_repeat_card_different_search_terms_is_one_update(self):
        report = self.run_writer([self.show(), self.show(searched_name="another search")])
        self.assertTrue(report.ok)
        self.assertEqual(report.matched, 1)
        self.assertEqual(len(self.posts[0]), 1)

    def test_conflicting_id_observations_remove_all_candidates(self):
        for changes in (dict(url="https://synthetic.invalid/card/2"),
                        dict(available_seats=29), dict(time="20:00"),
                        dict(hall="Other Hall"), dict(url=None), dict(url="")):
            with self.subTest(changes=changes):
                report = self.assert_rejected([self.show(), self.show(**changes), self.show()])
                self.assertEqual(len(report.ambiguous), 1)

    def test_same_id_for_distinct_dates_titles_or_orgs_is_rejected(self):
        original = dict(self.rows[0])
        cases = (
            ({"תאריך": "2026-10-21"}, {"date": "21/10/2026"}),
            ({"הפקה": "Other title"}, {"name": "Other title"}),
            ({"ארגון": "פרינדס"}, {"site_tag": "Friends"}),
        )
        for row_changes, show_changes in cases:
            with self.subTest(changes=show_changes):
                self.rows = [original, dict(original, **row_changes)]
                report = self.assert_rejected([self.show(), self.show(**show_changes)])
                self.assertEqual(len(report.ambiguous), 1)

    def test_missing_source_identity_rejected(self):
        self.assert_rejected([self.show(url=None)])

    def test_missing_names_do_not_stop_valid_record(self):
        shows = []
        for field in ("name", "searched_name"):
            for value in (None, "", "   "):
                shows.append(self.show(**{field: value}))
            missing = self.show()
            del missing[field]
            shows.append(missing)
        report = self.run_writer(shows + [self.show()])
        self.assertTrue(report.ok)
        self.assertEqual(report.matched, 1)
        self.assertEqual(len(report.skipped), 8)
        self.assertEqual(len(self.posts[0]), 1)

    def test_special_exclusions_and_simba_rewrite(self):
        for title in ("בת הים הקטנה", "בת ים הקטנה", "סימבה סוואנה", "פיטר פן הפקת הענק"):
            self.rows[0]["הפקה"] = title
            self.assert_rejected([self.show(name=title)])
        self.rows[0]["הפקה"] = "סימבה מלך"
        report = self.run_writer([self.show(name="סימבה")])
        self.assertTrue(report.ok)
        self.assertEqual(report.matched, 1)


if __name__ == "__main__":
    unittest.main()
