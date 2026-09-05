"""
Date range resolution: convert user-specified DateRangeType and context into canonical [start, end) half-open ranges.

Key insight: "last month" with "today = 2026-09-05" means August 2026 in full (2026-08-01 to 2026-09-01),
not "the last 30 days" or "2026-08-05 to 2026-09-05".
"""

from datetime import datetime, timedelta, date
from enum import Enum
from typing import Optional

from app.schemas.financial_query import DateRangeType, DateRange


APP_TIMEZONE = "Asia/Kolkata"


def today_ist() -> date:
    """Get today's date in IST."""
    from datetime import datetime, timezone, timedelta
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).date()


def resolve_date_range(spec: DateRangeType, context_date: date | None = None, month: str | None = None, year: int | None = None) -> DateRange:
    """
    Resolve a DateRangeType specification to a DateRange.
    
    Args:
        spec: The date range type (calendar_month, last_n_days, etc.)
        context_date: Reference date (default: today in IST)
        month: For calendar_month and named_month specs
        year: For named_month spec
    
    Returns:
        DateRange with start, end (exclusive), and label
    """
    if context_date is None:
        context_date = today_ist()
    
    if spec == DateRangeType.CALENDAR_MONTH:
        # "calendar_month" with month name = specific month in last year if month > this month, else this year
        if month:
            return _resolve_named_month(month, year, context_date)
        else:
            # No month specified: use last completed month
            return _last_completed_month(context_date)
    
    elif spec == DateRangeType.LAST_N_MONTHS:
        # "last N months" = last N completed calendar months, anchored at last completed month
        n = 1  # TODO: extract from kwargs
        end_month = _last_completed_month(context_date)
        start = end_month.start
        
        # Go back N months
        year_delta = (n - 1) // 12
        month_delta = (n - 1) % 12
        new_month = end_month.start.month - month_delta
        new_year = end_month.start.year - year_delta
        
        if new_month <= 0:
            new_month += 12
            new_year -= 1
        
        start_date = date(new_year, new_month, 1)
        end_date = end_month.end_exclusive  # One day after end
        label = f"last {n} months"
        
        return DateRange(start=start_date, end=end_date, label=label)
    
    elif spec == DateRangeType.CUSTOM:
        # User specified explicit dates
        # TODO: implement with start_date, end_date kwargs
        pass
    
    elif spec == DateRangeType.ALL_TIME:
        # All data
        return DateRange(start=date(1900, 1, 1), end=date(2100, 12, 31), label="all time")
    
    elif spec == DateRangeType.MONTH_BEFORE_PREVIOUS:
        # Two months ago (full calendar month)
        last = _last_completed_month(context_date)
        # Go back one more month
        prev_month = last.start.month - 1
        prev_year = last.start.year
        if prev_month <= 0:
            prev_month += 12
            prev_year -= 1
        
        start = date(prev_year, prev_month, 1)
        # Next month's 1st
        next_month = prev_month + 1
        next_year = prev_year
        if next_month > 12:
            next_month = 1
            next_year += 1
        end = date(next_year, next_month, 1)
        
        label = _month_label(start)
        return DateRange(start=start, end=end, label=label)
    
    elif spec == DateRangeType.THIS_MONTH:
        # 1st of this month to today
        start = date(context_date.year, context_date.month, 1)
        end = context_date + timedelta(days=1)
        label = f"{_month_name(context_date.month)} {context_date.year} (month to date)"
        return DateRange(start=start, end=end, label=label)
    
    elif spec == DateRangeType.THIS_WEEK:
        # ISO week: Monday to today
        monday = context_date - timedelta(days=context_date.weekday())
        end = context_date + timedelta(days=1)
        label = f"{_month_name(monday.month)} {monday.day} - {_month_name(context_date.month)} {context_date.day}"
        return DateRange(start=monday, end=end, label=label)
    
    elif spec == DateRangeType.LAST_WEEK:
        # Previous ISO week: Monday to Sunday
        today_weekday = context_date.weekday()
        last_sunday = context_date - timedelta(days=today_weekday + 1)  # Previous Sunday
        last_monday = last_sunday - timedelta(days=6)  # 6 days before Sunday
        end = last_sunday + timedelta(days=1)  # Up to and including Sunday
        label = f"{_month_name(last_monday.month)} {last_monday.day} - {_month_name(last_sunday.month)} {last_sunday.day}"
        return DateRange(start=last_monday, end=end, label=label)
    
    elif spec == DateRangeType.LAST_N_DAYS:
        # "last N days" = last N days inclusive
        n = 7  # TODO: extract from kwargs
        end = context_date + timedelta(days=1)  # Exclusive
        start = context_date - timedelta(days=n - 1)
        label = f"last {n} days"
        return DateRange(start=start, end=end, label=label)
    
    elif spec == DateRangeType.YESTERDAY:
        yesterday = context_date - timedelta(days=1)
        end = context_date
        label = "yesterday"
        return DateRange(start=yesterday, end=end, label=label)
    
    elif spec == DateRangeType.TODAY:
        end = context_date + timedelta(days=1)
        label = "today"
        return DateRange(start=context_date, end=end, label=label)
    
    elif spec == DateRangeType.THIS_YEAR:
        # Year-to-date
        start = date(context_date.year, 1, 1)
        end = context_date + timedelta(days=1)
        label = f"{context_date.year} (year to date)"
        return DateRange(start=start, end=end, label=label)
    
    elif spec == DateRangeType.LAST_YEAR:
        # Full previous calendar year
        start = date(context_date.year - 1, 1, 1)
        end = date(context_date.year, 1, 1)
        label = str(context_date.year - 1)
        return DateRange(start=start, end=end, label=label)
    
    else:
        raise ValueError(f"Unhandled DateRangeType: {spec}")


def _last_completed_month(ref_date: date) -> DateRange:
    """Return the last full calendar month before ref_date."""
    # If we're on Sep 5, last completed month is Aug (1-31)
    # If we're on Sep 1, last completed month is Aug
    first_of_this_month = date(ref_date.year, ref_date.month, 1)
    last_of_prev_month = first_of_this_month - timedelta(days=1)
    
    first_of_prev_month = date(last_of_prev_month.year, last_of_prev_month.month, 1)
    first_of_this_month = date(ref_date.year, ref_date.month, 1)
    
    label = _month_label(first_of_prev_month)
    return DateRange(start=first_of_prev_month, end=first_of_this_month, label=label)


def _resolve_named_month(month_name: str, year: int | None, ref_date: date) -> DateRange:
    """
    Resolve a named month like "August" or "Aug" to a DateRange.
    
    If year is not specified and the month is in the future (relative to ref_date),
    assume the previous year.
    
    Args:
        month_name: "August", "Aug", "august", etc.
        year: Calendar year, or None to infer
        ref_date: Reference date for inference
    
    Returns:
        DateRange for the full calendar month
    """
    # Parse month name
    month_num = _parse_month_name(month_name)
    if month_num is None:
        raise ValueError(f"Invalid month name: {month_name}")
    
    # Infer year if needed
    if year is None:
        year = ref_date.year
        # If the month is in the future (> current month), assume previous year
        if month_num > ref_date.month:
            year -= 1
    
    start = date(year, month_num, 1)
    # Next month's 1st
    next_month = month_num + 1
    next_year = year
    if next_month > 12:
        next_month = 1
        next_year += 1
    end = date(next_year, next_month, 1)
    
    label = f"{_MONTH_NAMES[month_num]} {year}"
    return DateRange(start=start, end=end, label=label)


def previous_period(original_period: DateRange) -> DateRange:
    """
    Get the period immediately before original_period.
    
    For full calendar months, returns the previous month.
    For arbitrary ranges, returns an equal-length window.
    """
    duration = (original_period.end - original_period.start).days
    
    # Check if it's a full calendar month
    if (original_period.start.day == 1 and 
        (original_period.start.month % 12) + 1 == original_period.end.month or
        (original_period.start.month == 12 and original_period.end.month == 1)):
        # Full calendar month: go back one month
        prev_start = original_period.start.replace(day=1)
        prev_month = prev_start.month - 1
        prev_year = prev_start.year
        if prev_month <= 0:
            prev_month += 12
            prev_year -= 1
        
        prev_start = date(prev_year, prev_month, 1)
        prev_end = original_period.start
        label = _month_label(prev_start)
    else:
        # Arbitrary range: go back by same duration
        prev_end = original_period.start
        prev_start = prev_end - timedelta(days=duration)
        label = f"{prev_start} to {prev_end - timedelta(days=1)}"
    
    return DateRange(start=prev_start, end=prev_end, label=label)


_MONTH_NAMES = {
    1: "January",
    2: "February",
    3: "March",
    4: "April",
    5: "May",
    6: "June",
    7: "July",
    8: "August",
    9: "September",
    10: "October",
    11: "November",
    12: "December",
}

_MONTH_ABBREV = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def _parse_month_name(name: str) -> int | None:
    """Parse a month name (full or abbreviated) to 1-12."""
    return _MONTH_ABBREV.get(name.lower())


def _month_name(month_num: int) -> str:
    """Convert month 1-12 to full name."""
    return _MONTH_NAMES.get(month_num, "")


def _month_label(d: date) -> str:
    """Create a label for a date like 'August 2026'."""
    return f"{_month_name(d.month)} {d.year}"
