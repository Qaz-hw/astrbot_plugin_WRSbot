#=====================================================
#  utils/time.py — Time and Date Utilities
#=====================================================
#
#  Responsibilities:
#    - Week range calculation (Monday to Sunday)
#    - UTC+8 datetime formatting for display
#    - Week label generation for report naming
#
#  All functions are pure — no external dependencies
#  beyond Python's standard datetime library.
#=====================================================

## Dependencies
# from datetime import datetime, date, timedelta, timezone

## get_current_week_range
# get_current_week_range() -> tuple[date, date]
#     # return (monday, sunday) of the current ISO week
#     # based on UTC+8 local time

## format_utc8
# format_utc8(dt: datetime) -> str
#     # format a datetime as "YYYY-MM-DD HH:MM:SS (UTC+8)"
#     # used in card variables and log messages

## week_label
# week_label(start: date) -> str
#     # return ISO week label from a Monday date
#     # e.g. date(2026, 5, 11) → "2026-W20"
