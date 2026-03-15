from collections import defaultdict

from .models import TimetableBatch, TimetableEntry, db


DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
CLASS_TIME_SLOTS = [
    "09:00 - 09:50",
    "09:50 - 10:40",
    "10:40 - 11:30",
    "11:50 - 12:40",
    "12:40 - 13:30",
    "14:10 - 15:00",
    "15:00 - 15:50",
]
TIMETABLE_COLUMNS = [
    {"type": "class", "label": "09:00 - 09:50", "value": "09:00 - 09:50"},
    {"type": "class", "label": "09:50 - 10:40", "value": "09:50 - 10:40"},
    {"type": "class", "label": "10:40 - 11:30", "value": "10:40 - 11:30"},
    {"type": "break", "label": "Break<br>11:30 - 11:50", "value": "BREAK"},
    {"type": "class", "label": "11:50 - 12:40", "value": "11:50 - 12:40"},
    {"type": "class", "label": "12:40 - 13:30", "value": "12:40 - 13:30"},
    {"type": "break", "label": "Lunch<br>13:30 - 14:10", "value": "LUNCH"},
    {"type": "class", "label": "14:10 - 15:00", "value": "14:10 - 15:00"},
    {"type": "class", "label": "15:00 - 15:50", "value": "15:00 - 15:50"},
]


def build_timetable(registrations, batch_id):
    faculty_busy = set()
    section_busy = set()
    section_subject_day_busy = set()
    generated_entries = []
    unassigned = []

    grouped = defaultdict(list)
    for registration in registrations:
        grouped[registration.preferred_section].append(registration)

    for section, section_registrations in grouped.items():
        for registration in section_registrations:
            needed_slots = registration.subject.weekly_slots
            assigned = 0

            for day in DAYS:
                if assigned >= needed_slots:
                    break

                for time_slot in CLASS_TIME_SLOTS:
                    faculty_key = (registration.faculty_id, day, time_slot)
                    section_key = (section, day, time_slot)
                    section_subject_day_key = (section, registration.subject_id, day)

                    if (
                        faculty_key in faculty_busy
                        or section_key in section_busy
                        or section_subject_day_key in section_subject_day_busy
                    ):
                        continue

                    generated_entries.append(
                        TimetableEntry(
                            batch_id=batch_id,
                            day=day,
                            time_slot=time_slot,
                            section=section,
                            faculty_id=registration.faculty_id,
                            subject_id=registration.subject_id,
                        )
                    )
                    faculty_busy.add(faculty_key)
                    section_busy.add(section_key)
                    section_subject_day_busy.add(section_subject_day_key)
                    assigned += 1

                    if assigned >= needed_slots:
                        break

            if assigned < needed_slots:
                unassigned.append(
                    {
                        "faculty": registration.faculty.name,
                        "subject": registration.subject.name,
                        "section": registration.preferred_section,
                        "remaining_slots": needed_slots - assigned,
                    }
                )

    return generated_entries, unassigned


def create_timetable_batch(registrations, semester):
    batch = TimetableBatch(
        name=f"Semester {semester} Timetable",
        semester=semester,
    )
    db.session.add(batch)
    db.session.flush()

    entries, unassigned = build_timetable(registrations, batch.id)
    db.session.add_all(entries)
    db.session.commit()
    return batch, entries, unassigned
