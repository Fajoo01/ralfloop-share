from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import ralf_bottazzi_calendar_feed as feed


def test_all_day_ics_uses_value_date():
    data = feed._ics("t@example", "All day", date(2026, 10, 20), date(2026, 10, 21), "", True).decode()
    assert "DTSTART;VALUE=DATE:20261020" in data
    assert "DTEND;VALUE=DATE:20261021" in data


def test_timed_ics_is_normalized_to_utc():
    tz = timezone(timedelta(hours=2))
    start = datetime(2026, 10, 20, 10, 30, tzinfo=tz)
    end = datetime(2026, 10, 20, 11, 30, tzinfo=tz)
    data = feed._ics("t@example", "Timed", start, end, "", False).decode()
    assert "DTSTART:20261020T083000Z" in data
    assert "DTEND:20261020T093000Z" in data


def test_frontend_contains_google_calendar_core_views():
    html = (ROOT / "web/bottazzi-calendar/index.html").read_text()
    js = (ROOT / "web/bottazzi-calendar/app.js").read_text()
    assert all(f'data-view="{view}"' in html for view in ("day", "week", "month", "agenda"))
    assert 'id="eventAllDay"' in html
    assert "layoutTimedSegments" in js and "makeAllDayRow" in js


def test_android_app_is_only_a_shell_for_the_web_calendar():
    main = (ROOT / "android/bottazzi-calendar/app/src/main/java/org/tiremminnanz/bottazzi/calendar/MainActivity.java").read_text()
    gradle = (ROOT / "android/bottazzi-calendar/app/build.gradle").read_text()
    assert "new WebView(this)" in main
    assert "webView.loadUrl(BuildConfig.CALENDAR_URL)" in main
    assert "TextView header" not in main
    assert "versionName '0.4.0'" in gradle
