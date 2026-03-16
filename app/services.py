import random
from collections import defaultdict

from .models import TimetableBatch, TimetableEntry, db


DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
ACADEMIC_DAY_START = 9 * 60
ACADEMIC_DAY_END = 16 * 60
TEACHING_SLOT_MINUTES = 50
LUNCH_BREAK_MINUTES = 40
TEA_BREAK_MINUTES = 30


def format_minutes(total_minutes):
    hours = total_minutes // 60
    minutes = total_minutes % 60
    return f"{hours:02d}:{minutes:02d}"


def build_slot(start_minutes, duration_minutes, slot_number, is_break, break_name=None):
    end_minutes = start_minutes + duration_minutes
    start_time = format_minutes(start_minutes)
    end_time = format_minutes(end_minutes)
    label = f"{start_time} - {end_time}"
    if is_break:
        slot_key = (break_name or "BREAK").upper().replace(" ", "_")
        display_name = (break_name or "Break").title()
        return {
            "slot_number": slot_number,
            "start_time": start_time,
            "end_time": end_time,
            "is_break": True,
            "break_name": display_name,
            "type": "break",
            "value": slot_key,
            "label": f"{display_name}<br>{label}",
        }

    return {
        "slot_number": slot_number,
        "start_time": start_time,
        "end_time": end_time,
        "is_break": False,
        "break_name": None,
        "type": "class",
        "value": label,
        "label": label,
    }


def lunch_break_after_slot(semester):
    return 3 if semester % 2 == 0 else 4


def get_slot_template(semester):
    teaching_slots = []
    slot_template = []
    current_time = ACADEMIC_DAY_START
    lunch_after_slot = lunch_break_after_slot(semester)

    for slot_number in range(1, 8):
        class_slot = build_slot(
            start_minutes=current_time,
            duration_minutes=TEACHING_SLOT_MINUTES,
            slot_number=slot_number,
            is_break=False,
        )
        slot_template.append(class_slot)
        teaching_slots.append(class_slot["value"])
        current_time += TEACHING_SLOT_MINUTES

        if slot_number == lunch_after_slot:
            slot_template.append(
                build_slot(
                    start_minutes=current_time,
                    duration_minutes=LUNCH_BREAK_MINUTES,
                    slot_number=slot_number,
                    is_break=True,
                    break_name="Lunch",
                )
            )
            current_time += LUNCH_BREAK_MINUTES

        if slot_number == 6:
            slot_template.append(
                build_slot(
                    start_minutes=current_time,
                    duration_minutes=TEA_BREAK_MINUTES,
                    slot_number=slot_number,
                    is_break=True,
                    break_name="Tea Break",
                )
            )
            current_time += TEA_BREAK_MINUTES

    if current_time != ACADEMIC_DAY_END:
        raise ValueError("The academic slot template must end at 16:00.")

    return slot_template, teaching_slots


def get_timetable_columns(semester):
    slot_template, _ = get_slot_template(semester)
    return slot_template


def initialize_schedule_environment(semester, section):
    slot_template, teaching_slots = get_slot_template(semester)
    weekly_matrix = {
        day: {slot["value"]: None for slot in slot_template if not slot["is_break"]}
        for day in DAYS
    }
    return {
        "working_days": list(DAYS),
        "academic_day": {
            "start_time": format_minutes(ACADEMIC_DAY_START),
            "end_time": format_minutes(ACADEMIC_DAY_END),
        },
        "slot_template": slot_template,
        "teaching_slots": teaching_slots,
        "section": section,
        "weekly_matrix": weekly_matrix,
    }


def required_theory_slots(subject):
    return max(getattr(subject, "theory_lectures_per_week", subject.weekly_slots) or 0, 0)


def required_lab_sessions(subject):
    if getattr(subject, "has_lab", False):
        return max(getattr(subject, "lab_sessions_per_week", 0) or 0, 0)
    if getattr(subject, "is_lab", False):
        return max(subject.weekly_slots or 0, 0)
    return 0


def build_lab_block_candidates(environment):
    candidates = []

    for day in environment["working_days"]:
        day_schedule = environment["weekly_matrix"][day]
        for index in range(len(environment["slot_template"]) - 1):
            current_slot = environment["slot_template"][index]
            next_slot = environment["slot_template"][index + 1]
            if current_slot["is_break"] or next_slot["is_break"]:
                continue
            if day_schedule[current_slot["value"]] or day_schedule[next_slot["value"]]:
                continue
            candidates.append(
                {
                    "day": day,
                    "slots": (current_slot["value"], next_slot["value"]),
                }
            )

    random.shuffle(candidates)
    return candidates


def can_allocate_faculty_load(faculty_loads, faculty, additional_slots):
    max_weekly_load = getattr(faculty, "max_weekly_load", 0) or 0
    if max_weekly_load <= 0:
        return True
    return faculty_loads[faculty.id] + additional_slots <= max_weekly_load


def assign_entry(
    batch_id,
    section,
    registration,
    day,
    time_slot,
    environment,
    generated_entries,
    faculty_busy,
    section_busy,
    faculty_loads,
):
    entry = TimetableEntry(
        batch_id=batch_id,
        day=day,
        time_slot=time_slot,
        section=section,
        faculty_id=registration.faculty_id,
        subject_id=registration.subject_id,
    )
    generated_entries.append(entry)
    environment["weekly_matrix"][day][time_slot] = entry
    faculty_busy.add((registration.faculty_id, day, time_slot))
    section_busy.add((section, day, time_slot))
    faculty_loads[registration.faculty_id] += 1


def allocate_lab_sessions(
    section,
    registrations,
    environment,
    batch_id,
    generated_entries,
    faculty_busy,
    section_busy,
    faculty_loads,
):
    unassigned = []
    lab_day_counts = defaultdict(int)
    ordered_labs = sorted(
        [
            registration
            for registration in registrations
            if required_lab_sessions(registration.subject) > 0
        ],
        key=lambda registration: (
            not registration.subject.is_subject_linked_lab,
            registration.subject.code,
            registration.faculty.name,
        ),
    )

    for registration in ordered_labs:
        required_sessions = required_lab_sessions(registration.subject)
        assigned_sessions = 0

        while assigned_sessions < required_sessions:
            candidates = build_lab_block_candidates(environment)
            if not candidates:
                break

            candidates.sort(key=lambda candidate: lab_day_counts[candidate["day"]])
            placed = False

            for candidate in candidates:
                day = candidate["day"]
                slot_one, slot_two = candidate["slots"]
                if (
                    (registration.faculty_id, day, slot_one) in faculty_busy
                    or (registration.faculty_id, day, slot_two) in faculty_busy
                    or (section, day, slot_one) in section_busy
                    or (section, day, slot_two) in section_busy
                    or not can_allocate_faculty_load(faculty_loads, registration.faculty, 2)
                ):
                    continue

                assign_entry(
                    batch_id=batch_id,
                    section=section,
                    registration=registration,
                    day=day,
                    time_slot=slot_one,
                    environment=environment,
                    generated_entries=generated_entries,
                    faculty_busy=faculty_busy,
                    section_busy=section_busy,
                    faculty_loads=faculty_loads,
                )
                assign_entry(
                    batch_id=batch_id,
                    section=section,
                    registration=registration,
                    day=day,
                    time_slot=slot_two,
                    environment=environment,
                    generated_entries=generated_entries,
                    faculty_busy=faculty_busy,
                    section_busy=section_busy,
                    faculty_loads=faculty_loads,
                )
                lab_day_counts[day] += 1
                assigned_sessions += 1
                placed = True
                break

            if not placed:
                break

        if assigned_sessions < required_sessions:
            unassigned.append(
                {
                    "faculty": registration.faculty.name,
                    "subject": registration.subject.name,
                    "section": registration.preferred_section,
                    "remaining_slots": required_sessions - assigned_sessions,
                    "type": "lab",
                }
            )

    return unassigned


def allocate_theory_sessions(
    section,
    registrations,
    environment,
    batch_id,
    generated_entries,
    faculty_busy,
    section_busy,
    section_subject_day_busy,
    faculty_loads,
):
    unassigned = []
    ordered_registrations = sorted(
        registrations,
        key=lambda registration: (
            not getattr(registration.subject, "is_priority", False),
            registration.subject.code,
            registration.faculty.name,
        ),
    )

    for registration in ordered_registrations:
        needed_slots = required_theory_slots(registration.subject)
        assigned = 0

        for day in environment["working_days"]:
            if assigned >= needed_slots:
                break

            for time_slot in environment["teaching_slots"]:
                faculty_key = (registration.faculty_id, day, time_slot)
                section_key = (section, day, time_slot)
                section_subject_day_key = (section, registration.subject_id, day)

                if (
                    faculty_key in faculty_busy
                    or section_key in section_busy
                    or section_subject_day_key in section_subject_day_busy
                    or environment["weekly_matrix"][day][time_slot] is not None
                    or not can_allocate_faculty_load(faculty_loads, registration.faculty, 1)
                ):
                    continue

                assign_entry(
                    batch_id=batch_id,
                    section=section,
                    registration=registration,
                    day=day,
                    time_slot=time_slot,
                    environment=environment,
                    generated_entries=generated_entries,
                    faculty_busy=faculty_busy,
                    section_busy=section_busy,
                    faculty_loads=faculty_loads,
                )
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
                    "type": "theory",
                }
            )

    return unassigned


def build_timetable(registrations, batch_id):
    faculty_busy = set()
    faculty_loads = defaultdict(int)
    section_busy = set()
    section_subject_day_busy = set()
    generated_entries = []
    unassigned = []

    grouped = defaultdict(list)
    for registration in registrations:
        grouped[registration.preferred_section].append(registration)

    for section, section_registrations in grouped.items():
        environment = initialize_schedule_environment(
            semester=section_registrations[0].subject.semester,
            section=section,
        )
        unassigned.extend(
            allocate_lab_sessions(
                section=section,
                registrations=section_registrations,
                environment=environment,
                batch_id=batch_id,
                generated_entries=generated_entries,
                faculty_busy=faculty_busy,
                section_busy=section_busy,
                faculty_loads=faculty_loads,
            )
        )
        unassigned.extend(
            allocate_theory_sessions(
                section=section,
                registrations=[
                    registration
                    for registration in section_registrations
                    if required_theory_slots(registration.subject) > 0
                ],
                environment=environment,
                batch_id=batch_id,
                generated_entries=generated_entries,
                faculty_busy=faculty_busy,
                section_busy=section_busy,
                section_subject_day_busy=section_subject_day_busy,
                faculty_loads=faculty_loads,
            )
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
