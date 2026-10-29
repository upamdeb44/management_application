from datetime import date, datetime, timedelta
import calendar as cal
from database import (
    get_all_works, mark_completed, mark_occurrence_completed, get_completed_dates,
    claim_reminder
)

# How far each kind of repeat is allowed to run, and what the "Repeat until" default is.
# (A daily task for 10 years would be thousands of calendar entries, so daily is capped lower.)
MAX_SPAN_DAYS = {"Every day": 730, "Every week": 1825, "Every month": 3650, "Every year": 10950}
DEFAULT_SPAN_DAYS = {"Every day": 30, "Every week": 90, "Every month": 365, "Every year": 1095}


def max_span_days(recurrence_type):
    return MAX_SPAN_DAYS.get(recurrence_type, 730)


def default_span_days(recurrence_type):
    return DEFAULT_SPAN_DAYS.get(recurrence_type, 30)


def parse_due_datetime(due_date, due_time):
    if not due_date:
        return None
    if not due_time:
        due_time = "06:00 PM"
    formats = ["%Y-%m-%d %I:%M %p", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"]
    for fmt in formats:
        try:
            return datetime.strptime(f"{due_date} {due_time}", fmt)
        except ValueError:
            pass
    return None


def format_due_time(due_time):
    if not due_time:
        return "06:00 PM"
    if isinstance(due_time, str):
        for fmt in ["%I:%M %p", "%H:%M:%S", "%H:%M"]:
            try:
                return datetime.strptime(due_time, fmt).strftime("%I:%M %p")
            except ValueError:
                pass
    return str(due_time)


def set_occurrence_status(task_id, user_id, occurrence_date, recurrence_type, completed):
    if recurrence_type == "Does not repeat":
        mark_completed(task_id, user_id, completed)
    else:
        mark_occurrence_completed(task_id, user_id, occurrence_date.isoformat(), completed)


def generate_occurrences(work, user_id):
    (task_id, title, description, due_date, due_time, category, priority,
     reminder_3_days, reminder_2_days, reminder_1_day, reminder_1_hour, completed,
     recurrence_type, recurrence_interval, recurrence_days, recurrence_end_date) = work[:16]
    reminder_at_time = work[16] if len(work) > 16 else 0
    reminder_custom_minutes = work[17] if len(work) > 17 else 0

    start_date = datetime.strptime(due_date[:10], "%Y-%m-%d").date()
    recurrence_type = recurrence_type or "Does not repeat"

    try:
        interval = int(recurrence_interval or 1)
    except (ValueError, TypeError):
        interval = 1
    if interval < 1:
        interval = 1

    max_date = start_date + timedelta(days=max_span_days(recurrence_type))
    if recurrence_end_date:
        try:
            end_date = datetime.strptime(recurrence_end_date, "%Y-%m-%d").date()
        except ValueError:
            end_date = max_date
    else:
        end_date = max_date
    end_date = min(end_date, max_date)

    # One query per task (instead of one per occurrence)
    if recurrence_type == "Does not repeat":
        done_dates = set()
    else:
        done_dates = get_completed_dates(task_id, user_id)

    occurrences = []
    formatted_time = format_due_time(due_time)

    def add_occ(curr_date, status):
        occurrences.append({
            "task_id": task_id, "title": title, "description": description,
            "date": curr_date, "due_time": formatted_time,
            "category": category, "priority": priority, "completed": status,
            "recurrence_type": recurrence_type, "reminder_3_days": reminder_3_days,
            "reminder_2_days": reminder_2_days, "reminder_1_day": reminder_1_day,
            "reminder_1_hour": reminder_1_hour,
            "reminder_at_time": reminder_at_time,
            "reminder_custom_minutes": reminder_custom_minutes,
        })

    if recurrence_type == "Does not repeat":
        add_occ(start_date, bool(completed))
        return occurrences

    if recurrence_type in ("Every day", "Every week"):
        step = timedelta(days=interval) if recurrence_type == "Every day" else timedelta(weeks=interval)
        current_date = start_date
        while current_date <= end_date:
            add_occ(current_date, current_date.isoformat() in done_dates)
            current_date += step
        return occurrences

    if recurrence_type == "Every month":
        month_index = start_date.year * 12 + start_date.month - 1
        while True:
            year = month_index // 12
            month = (month_index % 12) + 1
            last_day = cal.monthrange(year, month)[1]
            occurrence_date = date(year, month, min(start_date.day, last_day))
            if occurrence_date > end_date:
                break
            if occurrence_date >= start_date:
                add_occ(occurrence_date, occurrence_date.isoformat() in done_dates)
            month_index += interval
        return occurrences

    if recurrence_type == "Every year":
        current_year = start_date.year
        while current_year <= end_date.year:
            try:
                occurrence_date = date(current_year, start_date.month, start_date.day)
            except ValueError:  # Feb 29 on a non-leap year
                occurrence_date = date(current_year, start_date.month, 28)
            if start_date <= occurrence_date <= end_date:
                add_occ(occurrence_date, occurrence_date.isoformat() in done_dates)
            current_year += interval
        return occurrences

    return occurrences


def get_all_occurrences(user_id):
    works = get_all_works(user_id)
    all_occurrences = []
    for work in works:
        all_occurrences.extend(generate_occurrences(work, user_id))
    all_occurrences.sort(
        key=lambda o: parse_due_datetime(o["date"].isoformat(), o["due_time"]) or datetime.max
    )
    return all_occurrences


def get_reminders_for_occurrence(occurrence):
    due_datetime = parse_due_datetime(occurrence["date"].isoformat(), occurrence["due_time"])
    if not due_datetime:
        return []
    reminders = []
    if occurrence["reminder_3_days"]:
        reminders.append((due_datetime - timedelta(days=3), "3 days before"))
    if occurrence["reminder_2_days"]:
        reminders.append((due_datetime - timedelta(days=2), "2 days before"))
    if occurrence["reminder_1_day"]:
        reminders.append((due_datetime - timedelta(days=1), "1 day before"))
    if occurrence["reminder_1_hour"]:
        reminders.append((due_datetime - timedelta(hours=1), "1 hour before"))
    minutes = int(occurrence.get("reminder_custom_minutes") or 0)
    if minutes > 0:
        reminders.append((due_datetime - timedelta(minutes=minutes), f"{minutes} min before"))
    if occurrence.get("reminder_at_time"):
        reminders.append((due_datetime, "At due time"))
    return reminders


# Still alert this long after the due time (e.g. the app was closed at the moment it was due)
DUE_GRACE = timedelta(hours=1)


def describe_time_left(due, now):
    at = due.strftime('%d %b, %I:%M %p')
    if due <= now:
        minutes = int((now - due).total_seconds() // 60)
        when = "due now" if minutes < 1 else f"was due {minutes} min ago"
        return f"{when} (at {at})"
    minutes = max(int((due - now).total_seconds() // 60), 1)
    if minutes < 60:
        rel = f"in {minutes} min"
    elif minutes < 24 * 60:
        hours, mins = divmod(minutes, 60)
        rel = f"in {hours}h {mins}m" if mins else f"in {hours}h"
    else:
        days = minutes // (24 * 60)
        rel = f"in {days} day" + ("s" if days != 1 else "")
    return f"due {rel} (at {at})"


def fire_due_reminders(user_id):
    """
    Find reminders whose time has arrived (and whose task isn't done), record them so they
    never fire twice, and return the NEW ones as alert dicts.
    If several reminders for one task are due at once, only the latest one alerts.
    """
    now = datetime.now()
    today = now.date()
    alerts = []

    for occ in get_all_occurrences(user_id):
        if occ["completed"]:
            continue
        # the longest reminder is 3 days, so only nearby occurrences can be due
        if occ["date"] < today - timedelta(days=1) or occ["date"] > today + timedelta(days=4):
            continue
        due = parse_due_datetime(occ["date"].isoformat(), occ["due_time"])
        if not due or now > due + DUE_GRACE:   # long past due: shows under "Overdue" instead
            continue

        due_reminders = sorted(r for r in get_reminders_for_occurrence(occ) if r[0] <= now)
        if not due_reminders:
            continue

        message = describe_time_left(due, now)
        occ_date = occ["date"].isoformat()

        for _, label in due_reminders[:-1]:   # older reminders: mark as handled silently
            claim_reminder(user_id, occ["task_id"], occ_date, label, occ["title"], message, silent=True)

        _, last_label = due_reminders[-1]
        if claim_reminder(user_id, occ["task_id"], occ_date, last_label, occ["title"], message):
            alerts.append({
                "title": occ["title"], "message": message,
                "tag": f"{occ['task_id']}_{occ_date}_{last_label}",
            })
    return alerts