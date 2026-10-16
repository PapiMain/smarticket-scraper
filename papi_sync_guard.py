"""
papi_sync_guard.py — שכבת בטיחות משותפת לסקרייפרים של פאפי (ניטור מכירות ב-AppSheet).

נכתב 10/10/2026. מרכז במקום אחד את ארבעת הכללים שסוכמו:

  1. נתון חסר לא הופך לאפס. ערך לא תקין פוסל את השורה מהעדכון.
  2. הצלחה נקבעת לפי תוכן התגובה ומספר השורות שאושרו, לא לפי קוד HTTP.
  3. התאמה מתבססת על ערך מנורמל מדויק או על כינוי מוצהר — לא על הכלה חלקית.
  4. ירידה חריגה בנפח עוצרת את הכתיבה ומתריעה.

אין בקובץ הזה שום קריאת רשת מלבד אימות תגובה שכבר התקבלה.
"""

import json
import os
import re
import unicodedata


# ─────────────────────────────────────────────────────────────────────
# 1. ערכים חסרים
# ─────────────────────────────────────────────────────────────────────

MISSING = None  # ערך חסר מיוצג כ-None בלבד. לעולם לא 0.


def safe_int(value):
    """
    מחזיר int אם הערך הוא מספר שלם תקין ולא שלילי, אחרת None.

    None פירושו "לא ידוע" — והשורה תיפסל מהעדכון.
    אסור להחליף None ב-0: אפס הוא נתון אמיתי (לא נמכרו כרטיסים),
    וכתיבתו במקום ערך חסר מוחקת מידע אמיתי.
    """
    if value is None:
        return MISSING
    if isinstance(value, bool):          # bool הוא תת-מחלקה של int בפייתון
        return MISSING
    if isinstance(value, int):
        return value if value >= 0 else MISSING
    text = str(value).strip()
    if not text:
        return MISSING
    text = text.replace(",", "")
    if not re.fullmatch(r"\d+", text):
        return MISSING
    return int(text)


def sold_from_capacity(capacity, available):
    """
    מחשב כמה נמכרו מתוך קיבולת ומקומות פנויים.

    מחזיר (sold, reason). sold הוא None כשאי אפשר לחשב בביטחון,
    ואז reason מסביר למה — לצורך הדוח.

    שלילי אינו "אפס": הוא סימן שאחד הנתונים שגוי, ולכן השורה נפסלת.
    """
    cap = safe_int(capacity)
    avail = safe_int(available)
    if cap is MISSING:
        return MISSING, "קיבולת חסרה או לא תקינה"
    if avail is MISSING:
        return MISSING, "ספירת מקומות פנויים נכשלה"
    if cap == 0:
        return MISSING, "קיבולת 0 — ככל הנראה שדה חסר ולא אולם ריק"
    sold = cap - avail
    if sold < 0:
        return MISSING, f"חישוב שלילי (קיבולת {cap}, פנויים {avail}) — נתון שגוי"
    return sold, ""


# ─────────────────────────────────────────────────────────────────────
# 2. נרמול והתאמת שמות
# ─────────────────────────────────────────────────────────────────────

_PUNCT = re.compile(r"[\"'`׳״.,;:!?()\[\]{}]")
_DASHES = re.compile(r"[–—‒−]")


def normalize_title(text):
    """נרמול לצורך השוואה: רווחים, מקפים, גרשיים, ניקוד ואותיות סופיות."""
    if text is None:
        return ""
    s = unicodedata.normalize("NFKC", str(text))
    s = _DASHES.sub("-", s)
    s = _PUNCT.sub("", s)
    s = re.sub(r"[֑-ׇ]", "", s)   # ניקוד וטעמים
    s = re.sub(r"\s+", " ", s)
    return s.strip().lower()


def titles_match(scraped, candidate, aliases=None):
    """
    התאמה מדויקת אחרי נרמול, או התאמה דרך טבלת כינויים מוצהרת.

    במכוון אין כאן הכלה חלקית: ההכלה היא שגרמה ל"אלאדין" להתאים
    ל"אלאדין ומנורת נס החנוכה". שם קצר שמוכל בשם ארוך הוא מופע אחר.
    """
    a = normalize_title(scraped)
    b = normalize_title(candidate)
    if not a or not b:
        return False
    if a == b:
        return True
    if aliases:
        group_a = aliases.get(a)
        group_b = aliases.get(b)
        if group_a and group_a == group_b:
            return True
    return False


def build_alias_index(groups):
    """
    הופך {"מזהה קבוצה": ["שם א", "שם ב"]} למפה מנורמלת שם→קבוצה.

    כל כינוי חייב להיות מוצהר במפורש. אין ניחוש.
    """
    index = {}
    for group_id, names in (groups or {}).items():
        for name in names:
            index[normalize_title(name)] = group_id
    return index


def find_single_match(candidates, predicate):
    """
    מחזיר (match, status):
      (row,  "ok")        — התאמה יחידה
      (None, "none")      — אין התאמה
      (None, "ambiguous") — יותר מהתאמה אחת

    ריבוי התאמות לעולם אינו נפתר בלקיחת הראשונה. שורה מעורפלת נפסלת ומדווחת.
    """
    found = [row for row in candidates if predicate(row)]
    if len(found) == 1:
        return found[0], "ok"
    if not found:
        return None, "none"
    return None, "ambiguous"


# ─────────────────────────────────────────────────────────────────────
# 3. אימות תגובת AppSheet
# ─────────────────────────────────────────────────────────────────────

def verify_appsheet_response(response, sent_count):
    """
    מחזיר (ok, confirmed, detail).

    ה-Action API של AppSheet מחזיר 200 גם כששורות בודדות נכשלו,
    ולכן קוד הסטטוס לבדו אינו עדות. נבדק גוף התגובה ומספר
    השורות שחזרו מאושרות.
    """
    if type(sent_count) is not int or sent_count <= 0:
        return False, 0, "Invalid sent_count"
    status = getattr(response, "status_code", None)
    if status != 200:
        detail = f"HTTP {status}" if type(status) is int else "Invalid HTTP status"
        return False, 0, detail

    try:
        payload = response.json()
    except Exception:
        return False, 0, "Invalid JSON response"

    rows = payload.get("Rows") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return False, 0, "Invalid Rows container"
    if any(not isinstance(row, dict) for row in rows):
        return False, 0, "Invalid Rows item"

    confirmed = len(rows)
    if confirmed != sent_count:
        return False, confirmed, f"נשלחו {sent_count} שורות, אושרו {confirmed}"
    return True, confirmed, ""


# ─────────────────────────────────────────────────────────────────────
# 4. הגנה מירידה חריגה בנפח
# ─────────────────────────────────────────────────────────────────────

class VolumeGuard:
    """
    עוצר כתיבה כשנפח הריצה נפל בחדות לעומת הריצה המוצלחת הקודמת.

    אזהרה חשובה: ברירת המחדל שומרת את המצב בקובץ מקומי.
    ב-GitHub Actions ה-runner נמחק בסוף כל ריצה, ולכן קובץ מקומי
    אינו מקור אמת. יש להצביע עם PAPI_STATE_PATH על אחסון מתמיד
    או להחליף את load/save באחסון ענן. כל עוד אין מצב קודם,
    ההגנה מדווחת שאינה פעילה במקום להעמיד פנים שהיא פעילה.
    """

    def __init__(self, name, min_ratio=0.7, min_rows=1, state_path=None):
        self.name = name
        self.min_ratio = min_ratio
        self.min_rows = min_rows
        self.state_path = state_path or os.environ.get(
            "PAPI_STATE_PATH", os.path.join(".papi-state", "volume.json")
        )
        self.durable = bool(os.environ.get("PAPI_STATE_PATH"))

    def _load(self):
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return {}

    def check(self, current_count):
        """
        מחזיר (allowed, message).
        allowed=False פירושו: לא לכתוב, לדווח, ולצאת בקוד שגיאה.
        """
        if current_count < self.min_rows:
            return False, f"נסרקו {current_count} שורות — מתחת למינימום {self.min_rows}"

        state = self._load().get(self.name) or {}
        previous = state.get("last_success_count")

        if previous is None:
            note = "אין מצב קודם — הגנת הירידה אינה פעילה בריצה הזו"
            if not self.durable:
                note += " (PAPI_STATE_PATH לא הוגדר; מצב מקומי אינו שורד runner)"
            return True, note

        threshold = previous * self.min_ratio
        if current_count < threshold:
            return False, (
                f"ירידה חריגה: {current_count} שורות מול {previous} בריצה המוצלחת "
                f"הקודמת (סף {threshold:.0f}). הכתיבה נעצרה."
            )
        return True, f"נפח תקין: {current_count} מול {previous}"

    def record_success(self, count):
        state = self._load()
        entry = state.get(self.name) or {}
        entry["last_success_count"] = count
        state[self.name] = entry
        try:
            directory = os.path.dirname(self.state_path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(self.state_path, "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False, indent=2)
        except Exception as exc:
            print(f"⚠️ לא ניתן לשמור מצב נפח: {exc}")


# ─────────────────────────────────────────────────────────────────────
# 5. דוח ריצה
# ─────────────────────────────────────────────────────────────────────

class RunReport:
    """אוסף מונים וסיבות, ומכריע אם הריצה הצליחה."""

    def __init__(self, source):
        self.source = source
        self.scraped = 0
        self.matched = 0
        self.confirmed = 0
        self.skipped = []     # (identifier, reason)
        self.unmatched = []   # (identifier, reason)
        self.ambiguous = []   # (identifier, reason)
        self.errors = []

    def skip(self, identifier, reason):
        self.skipped.append((identifier, reason))

    def miss(self, identifier, reason):
        self.unmatched.append((identifier, reason))

    def ambiguity(self, identifier, reason):
        self.ambiguous.append((identifier, reason))

    def error(self, message):
        self.errors.append(message)

    @property
    def ok(self):
        return not self.errors

    def summary(self):
        lines = [
            "",
            "═" * 58,
            f"דוח ריצה — {self.source}",
            "═" * 58,
            f"  נסרקו:            {self.scraped}",
            f"  הותאמו:           {self.matched}",
            f"  אושרו בכתיבה:     {self.confirmed}",
            f"  נפסלו:            {len(self.skipped)}",
            f"  ללא התאמה:        {len(self.unmatched)}",
            f"  התאמה מעורפלת:    {len(self.ambiguous)}",
            f"  שגיאות:           {len(self.errors)}",
        ]
        for title, rows in (
            ("נפסלו (נתון חסר או שגוי)", self.skipped),
            ("ללא התאמה", self.unmatched),
            ("התאמה מעורפלת — נדרשת הכרעה ידנית", self.ambiguous),
        ):
            if rows:
                lines.append("")
                lines.append(f"  ── {title} ──")
                for identifier, reason in rows:
                    lines.append(f"     • {identifier}: {reason}")
        if self.errors:
            lines.append("")
            lines.append("  ── שגיאות ──")
            for message in self.errors:
                lines.append(f"     • {message}")
        lines.append("═" * 58)
        return "\n".join(lines)
