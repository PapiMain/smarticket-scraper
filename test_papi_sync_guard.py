"""
בדיקות ל-papi_sync_guard. מריצים: python3 test_papi_sync_guard.py
אין כאן רשת ואין AppSheet — בדיקות טהורות על הלוגיקה.
"""
import json
import os
import sys
import tempfile
import unittest

from papi_sync_guard import (
    MISSING, RunReport, VolumeGuard, build_alias_index, find_single_match,
    normalize_title, safe_int, sold_from_capacity, titles_match,
    verify_appsheet_response,
)


class FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text else json.dumps(payload, ensure_ascii=False) if payload is not None else ""

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class TestMissingNeverBecomesZero(unittest.TestCase):
    def test_blank_and_junk_are_missing(self):
        for value in (None, "", "   ", "לא ידוע", "N/A", "12.5", "-3", True, False):
            self.assertIs(safe_int(value), MISSING, f"{value!r} should be MISSING")

    def test_real_numbers_parse(self):
        self.assertEqual(safe_int(0), 0)
        self.assertEqual(safe_int("0"), 0)
        self.assertEqual(safe_int("1,250"), 1250)
        self.assertEqual(safe_int(42), 42)

    def test_zero_is_a_real_value_not_a_substitute(self):
        # אפס אמיתי נשמר; ערך חסר לא הופך לאפס
        self.assertEqual(safe_int("0"), 0)
        self.assertIsNot(safe_int(""), 0)

    def test_missing_capacity_does_not_produce_zero_sold(self):
        sold, reason = sold_from_capacity(None, 10)
        self.assertIs(sold, MISSING)
        self.assertIn("קיבולת", reason)

    def test_capacity_zero_is_treated_as_missing_field(self):
        sold, reason = sold_from_capacity(0, 0)
        self.assertIs(sold, MISSING)

    def test_negative_result_is_rejected_not_clamped(self):
        sold, reason = sold_from_capacity(100, 150)
        self.assertIs(sold, MISSING)
        self.assertIn("שלילי", reason)

    def test_normal_calculation(self):
        sold, reason = sold_from_capacity(200, 50)
        self.assertEqual(sold, 150)
        self.assertEqual(reason, "")

    def test_sold_out_is_allowed(self):
        sold, _ = sold_from_capacity(200, 0)
        self.assertEqual(sold, 200)


class TestTitleMatching(unittest.TestCase):
    def test_substring_no_longer_matches(self):
        # הבאג המקורי: "אלאדין" התאים ל"אלאדין ומנורת נס החנוכה"
        self.assertFalse(titles_match("אלאדין", "אלאדין ומנורת נס החנוכה"))
        self.assertFalse(titles_match("אלאדין ומנורת נס החנוכה", "אלאדין"))

    def test_exact_after_normalization(self):
        self.assertTrue(titles_match("  אלאדין  ", "אלאדין"))
        self.assertTrue(titles_match("בת-הים", "בת–הים"))
        self.assertTrue(titles_match('סבא אליעזר והגזר', "סבא אליעזר והגזר"))

    def test_declared_alias_matches(self):
        aliases = build_alias_index({"simba": ["סימבה", "סימבה מלך"]})
        self.assertTrue(titles_match("סימבה", "סימבה מלך", aliases))

    def test_alias_does_not_leak_between_groups(self):
        aliases = build_alias_index({
            "aladdin": ["אלאדין"],
            "aladdin-hanukkah": ["אלאדין ומנורת נס החנוכה"],
        })
        self.assertFalse(titles_match("אלאדין", "אלאדין ומנורת נס החנוכה", aliases))

    def test_empty_never_matches(self):
        self.assertFalse(titles_match("", ""))
        self.assertFalse(titles_match(None, "אלאדין"))


class TestAmbiguity(unittest.TestCase):
    def test_single_match(self):
        rows = [{"n": "a"}, {"n": "b"}]
        row, status = find_single_match(rows, lambda r: r["n"] == "a")
        self.assertEqual(status, "ok")
        self.assertEqual(row["n"], "a")

    def test_no_match(self):
        rows = [{"n": "a"}]
        row, status = find_single_match(rows, lambda r: r["n"] == "z")
        self.assertEqual(status, "none")
        self.assertIsNone(row)

    def test_two_matches_is_ambiguous_not_first_wins(self):
        rows = [{"n": "a", "id": 1}, {"n": "a", "id": 2}]
        row, status = find_single_match(rows, lambda r: r["n"] == "a")
        self.assertEqual(status, "ambiguous")
        self.assertIsNone(row)


class TestResponseVerification(unittest.TestCase):
    def test_http_200_with_all_rows_is_success(self):
        ok, confirmed, detail = verify_appsheet_response(
            FakeResponse(200, {"Rows": [{"ID": 1}, {"ID": 2}]}), 2)
        self.assertTrue(ok)
        self.assertEqual(confirmed, 2)

    def test_http_200_with_fewer_rows_is_failure(self):
        ok, confirmed, detail = verify_appsheet_response(
            FakeResponse(200, {"Rows": [{"ID": 1}]}), 2)
        self.assertFalse(ok)
        self.assertEqual(confirmed, 1)
        self.assertIn("אושרו", detail)

    def test_http_200_with_empty_rows_is_failure(self):
        ok, confirmed, _ = verify_appsheet_response(FakeResponse(200, {"Rows": []}), 3)
        self.assertFalse(ok)
        self.assertEqual(confirmed, 0)

    def test_http_200_without_rows_field_is_failure(self):
        ok, _, detail = verify_appsheet_response(FakeResponse(200, {"ok": True}), 1)
        self.assertFalse(ok)
        self.assertIn("Rows", detail)

    def test_http_200_with_non_json_body_is_failure(self):
        ok, _, detail = verify_appsheet_response(
            FakeResponse(200, None, text="<html>error</html>"), 1)
        self.assertFalse(ok)
        self.assertIn("JSON", detail)

    def test_non_200_is_failure(self):
        ok, _, detail = verify_appsheet_response(FakeResponse(500, None, text="boom"), 1)
        self.assertFalse(ok)
        self.assertIn("500", detail)


class TestVolumeGuard(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "volume.json")

    def guard(self, **kw):
        return VolumeGuard("test", state_path=self.path, **kw)

    def test_first_run_allowed_but_reports_guard_inactive(self):
        allowed, message = self.guard().check(40)
        self.assertTrue(allowed)
        self.assertIn("אין מצב קודם", message)

    def test_stable_volume_allowed(self):
        g = self.guard()
        g.record_success(40)
        allowed, _ = self.guard().check(39)
        self.assertTrue(allowed)

    def test_sharp_drop_blocks_write(self):
        g = self.guard()
        g.record_success(40)
        allowed, message = self.guard().check(5)
        self.assertFalse(allowed)
        self.assertIn("ירידה חריגה", message)

    def test_empty_scrape_blocks_write(self):
        allowed, message = self.guard().check(0)
        self.assertFalse(allowed)

    def test_growth_allowed(self):
        g = self.guard()
        g.record_success(40)
        allowed, _ = self.guard().check(120)
        self.assertTrue(allowed)

    def test_idempotent_rerun_same_volume(self):
        g = self.guard()
        g.record_success(34)
        for _ in range(3):
            allowed, _ = self.guard().check(34)
            self.assertTrue(allowed)
            self.guard().record_success(34)


class TestRunReport(unittest.TestCase):
    def test_report_without_errors_is_ok(self):
        r = RunReport("בדיקה")
        r.scraped = 10
        r.matched = 9
        r.skip("מופע א", "קיבולת חסרה")
        self.assertTrue(r.ok)
        self.assertIn("נפסלו", r.summary())

    def test_report_with_error_is_not_ok(self):
        r = RunReport("בדיקה")
        r.error("כשל כתיבה")
        self.assertFalse(r.ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)
