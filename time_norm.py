import dateparser
import re
from datetime import datetime

# Map vague phrases → standard clock times
VAGUE_TIME_MAP = {
    "morning"          : "07:00",
    "every morning"    : "07:00",
    "after breakfast"  : "08:00",
    "breakfast"        : "08:00",
    "noon"             : "12:00",
    "afternoon"        : "14:00",
    "after lunch"      : "13:00",
    "lunch"            : "13:00",
    "evening"          : "18:00",
    "every evening"    : "18:00",
    "after dinner"     : "20:00",
    "dinner"           : "19:00",
    "night"            : "21:00",
    "at night"         : "21:00",
    "once at night"    : "21:00",
    "before bed"       : "22:00",
    "bedtime"          : "22:00",
    "after meals"      : "08:00",   # default to breakfast
    "with meals"       : "08:00",
    "every 8 hours"    : "08:00",   # first dose at 8am
    "breakfast, lunch, and dinner" : "08:00",
    "8 am and 8 pm"    : "08:00",   # first dose
}

# Map frequency → reminder times list
FREQ_TO_TIMES = {
    "once daily"       : ["08:00"],
    "every morning"    : ["07:00"],
    "every evening"    : ["18:00"],
    "once at night"    : ["21:00"],
    "twice a day"      : ["08:00", "20:00"],
    "twice daily"      : ["08:00", "20:00"],
    "three times daily": ["08:00", "14:00", "20:00"],
    "every 8 hours"    : ["08:00", "16:00", "00:00"],
    "four times daily" : ["08:00", "12:00", "16:00", "20:00"],
    "every 6 hours"    : ["06:00", "12:00", "18:00", "00:00"],
}

def normalize_time(time_str: str) -> str:
    """Convert any time string → HH:MM format"""
    if not time_str or time_str == "N/A":
        return "08:00"  # default

    time_str_clean = time_str.strip().lower()

    # 1. Check vague phrase map first
    if time_str_clean in VAGUE_TIME_MAP:
        return VAGUE_TIME_MAP[time_str_clean]

    # 2. Try dateparser for real clock times
    parsed = dateparser.parse(time_str, settings={
        "PREFER_DAY_OF_MONTH": "first",
        "RETURN_AS_TIMEZONE_AWARE": False
    })
    if parsed:
        return parsed.strftime("%H:%M")

    # 3. Fallback
    return "08:00"


def get_reminder_times(frequency: str, time_of_day: str) -> list:
    """Return list of HH:MM reminder times based on frequency + time"""
    freq_clean = frequency.strip().lower() if frequency else ""

    # If frequency maps to multiple times, use those
    if freq_clean in FREQ_TO_TIMES:
        return FREQ_TO_TIMES[freq_clean]

    # Otherwise normalize the time_of_day and return as single reminder
    return [normalize_time(time_of_day)]


def format_reminder_summary(drug, dose, frequency, time_of_day) -> dict:
    """Full normalized medication schedule"""
    reminder_times = get_reminder_times(frequency, time_of_day)
    return {
        "drug"           : drug,
        "dose"           : dose,
        "frequency"      : frequency,
        "time_of_day"    : time_of_day,
        "reminder_times" : reminder_times,
        "reminders_count": len(reminder_times),
        "next_reminder"  : reminder_times[0] if reminder_times else "08:00"
    }


# ── Test it ──────────────────────────────────────────────────────────────────
test_cases = [
    ("Aspirin",    "500mg",    "twice daily",      "after breakfast"),
    ("Metformin",  "1000mg",   "every morning",    "morning"),
    ("Amoxicillin","250mg",    "every 8 hours",    "breakfast, lunch, and dinner"),
    ("Paracetamol","500mg",    "once at night",    "before bed"),
    ("Lisinopril", "2mg",      "once daily",       "8:00 AM"),
    ("Warfarin",   "5mg",      "every evening",    "8 pm"),
]

print(f"{'Drug':<14} {'Dose':<8} {'Frequency':<20} {'Time':<15} → Reminder Times")
print("─" * 80)
for drug, dose, freq, time in test_cases:
    result = format_reminder_summary(drug, dose, freq, time)
    times  = ", ".join(result["reminder_times"])
    print(f"{drug:<14} {dose:<8} {freq:<20} {time:<15} → {times}")
