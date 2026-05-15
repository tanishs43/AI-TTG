import math
import random
from collections import defaultdict
from functools import lru_cache

from .models import TimetableBatch, TimetableEntry, db


DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

@lru_cache(maxsize=8)
def get_slot_template(semester):
    teaching_slots = []
    slot_template = []
    
    is_even = (semester % 2 == 0)
    
    if is_even:
        # Even year - 2,4
        raw_slots = [
            ("class", "09:00", "09:50", 1),
            ("class", "09:50", "10:40", 2),
            ("class", "10:40", "11:30", 3),
            ("break", "11:30", "12:10", "Lunch"),
            ("class", "12:10", "13:00", 4),
            ("class", "13:00", "13:45", 5),
            ("class", "13:45", "14:30", 6),
            ("break", "14:30", "14:40", "Tea Break"),
            ("class", "14:40", "15:20", 7),
            ("class", "15:20", "16:00", 8),
        ]
    else:
        # Odd year 1,3
        raw_slots = [
            ("class", "09:00", "09:50", 1),
            ("class", "09:50", "10:40", 2),
            ("class", "10:40", "11:30", 3),
            ("class", "11:30", "12:20", 4),
            ("break", "12:20", "13:00", "Lunch"),
            ("class", "13:00", "13:45", 5),
            ("class", "13:45", "14:30", 6),
            ("break", "14:30", "14:40", "Tea Break"),
            ("class", "14:40", "15:20", 7),
            ("class", "15:20", "16:00", 8),
        ]

    for type_, start_time, end_time, identifier in raw_slots:
        label = f"{start_time} - {end_time}"
        if type_ == "class":
            slot_info = {
                "slot_number": identifier,
                "start_time": start_time,
                "end_time": end_time,
                "is_break": False,
                "break_name": None,
                "type": "class",
                "value": label,
                "label": label,
            }
            slot_template.append(slot_info)
            teaching_slots.append(label)
        else:
            slot_key = identifier.upper().replace(" ", "_")
            slot_info = {
                "slot_number": None,
                "start_time": start_time,
                "end_time": end_time,
                "is_break": True,
                "break_name": identifier,
                "type": "break",
                "value": slot_key,
                "label": f"{start_time}<br>-<br>{end_time}",
            }
            slot_template.append(slot_info)

    return slot_template, teaching_slots


@lru_cache(maxsize=8)
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


def validate_timetable(entries, teaching_slots):
    """
    Strictly validates the generated timetable for any hard constraint violations.
    Returns (is_valid, errors)
    """
    errors = []
    # (faculty_id, day, time_slot) -> list of entries
    faculty_usage = defaultdict(list)
    # (room, day, time_slot) -> list of entries
    room_usage = defaultdict(list)
    # (section, batch, day, time_slot) -> list of entries
    batch_usage = defaultdict(list)
    
    for entry in entries:
        if entry.faculty_id:
            faculty_usage[(entry.faculty_id, entry.day, entry.time_slot)].append(entry)
        if entry.room:
            room_usage[(entry.room, entry.day, entry.time_slot)].append(entry)
        
        # Section/Batch validation
        # If lab_batch is None, it's a theory class for the WHOLE section
        if entry.lab_batch:
            batch_usage[(entry.section, entry.lab_batch, entry.day, entry.time_slot)].append(entry)
        else:
            # Theory class occupies all batches (A1, A2)
            batch_usage[(entry.section, "A1", entry.day, entry.time_slot)].append(entry)
            batch_usage[(entry.section, "A2", entry.day, entry.time_slot)].append(entry)

    # Check for faculty conflicts
    for (fid, day, slot), e_list in faculty_usage.items():
        if len(e_list) > 1:
            sections = ", ".join(set(e.section for e in e_list))
            errors.append(f"Faculty conflict: ID {fid} assigned to multiple classes in {sections} on {day} at {slot}")

    # Check for room conflicts
    for (room, day, slot), e_list in room_usage.items():
        if len(e_list) > 1:
            sections = ", ".join(set(e.section for e in e_list))
            errors.append(f"Room conflict: {room} assigned to multiple sections {sections} on {day} at {slot}")

    # Check for batch conflicts within same section
    for (sec, batch, day, slot), e_list in batch_usage.items():
        if len(e_list) > 1:
            # Use subject_id to avoid lazy-loading issues on uncommitted entries
            subj_ids = ", ".join(str(e.subject_id) for e in e_list)
            errors.append(f"Batch conflict: Section {sec} Batch {batch} has multiple assignments (subject_ids: {subj_ids}) on {day} at {slot}")

    return len(errors) == 0, errors


# ─────────────────────────────────────────────────────────────────────────────
# GREEDY FALLBACK SCHEDULER  (used when OR-Tools is not installed)
# Enforces the same hard constraints as the CP-SAT solver:
#   • Faculty cannot be double-booked across sections at the same (day, slot)
#   • Lab rooms can only host one batch at a time
#   • At most one theory lecture per subject per day (spread across week)
#   • Faculty weekly load cap is respected
# ─────────────────────────────────────────────────────────────────────────────

def _build_timetable_greedy(all_registrations_by_section, batch_id, room_config=None):
    """Pure-Python greedy scheduler — no OR-Tools dependency."""
    if room_config is None:
        room_config = {"default_room": None, "lab_room_1": "Lab-131", "lab_room_2": "Lab-132"}

    lab_rooms = [room_config.get("lab_room_1", "Lab-131"), room_config.get("lab_room_2", "Lab-132")]
    default_room = room_config.get("default_room")
    batch_labels = ["A1", "A2"]

    semester = all_registrations_by_section[0][0].subject.semester
    slot_template, teaching_slots = get_slot_template(semester)
    num_days = len(DAYS)
    num_slots = len(teaching_slots)

    # Build list of valid consecutive-slot pairs for labs
    valid_lab_starts = []
    for idx in range(len(slot_template) - 1):
        if slot_template[idx]["is_break"] or slot_template[idx + 1]["is_break"]:
            continue
        v1 = slot_template[idx]["value"]
        v2 = slot_template[idx + 1]["value"]
        s1 = teaching_slots.index(v1)
        s2 = teaching_slots.index(v2)
        if s2 == s1 + 1:
            valid_lab_starts.append(s1)

    # Global constraint trackers  (shared across all sections)
    # faculty_busy[(faculty_id, day_idx, slot_idx)] = True
    faculty_busy = {}
    # lab_room_busy[(room, day_idx, slot_idx)] = True
    lab_room_busy = {}
    # faculty weekly load used so far
    faculty_weekly_used = defaultdict(int)

    # Pre-load existing load from already-committed entries (other batches)
    from .models import TimetableEntry as _TE
    unique_fids = set()
    for sec_regs in all_registrations_by_section:
        for r in sec_regs:
            if r.faculty_id is not None:
                unique_fids.add(r.faculty_id)
    if unique_fids:
        existing_entries = _TE.query.filter(
            _TE.faculty_id.in_(unique_fids),
            _TE.batch_id != batch_id
        ).all()
        for entry in existing_entries:
            faculty_weekly_used[entry.faculty_id] += 1
            if entry.day in DAYS and entry.time_slot in teaching_slots:
                d = DAYS.index(entry.day)
                s = teaching_slots.index(entry.time_slot)
                faculty_busy[(entry.faculty_id, d, s)] = True

    generated_entries = []
    unassigned = []

    rng = random.Random()

    for sec_regs in all_registrations_by_section:
        if not sec_regs:
            continue
        section = sec_regs[0].preferred_section

        # Per-section slot occupancy: (batch_label or "theory", day_idx, slot_idx) = True
        # "theory" = full-section theory class (occupies both batches implicitly)
        sec_busy = {}  # (day_idx, slot_idx) -> True  (any usage in this section)

        # Group registrations by subject
        subj_regs = defaultdict(list)
        for r in sec_regs:
            subj_regs[r.subject_id].append(r)

        # Shuffle subjects for variety
        subj_ids = list(subj_regs.keys())
        rng.shuffle(subj_ids)

        # ── 1. Schedule LABS first (they're harder to place) ───────────────
        for subj_id in subj_ids:
            regs = subj_regs[subj_id]
            subject = regs[0].subject
            req_lab = required_lab_sessions(subject)
            if req_lab == 0:
                continue

            # Pick faculty for lab (first available active registration)
            lab_faculty_id = None
            for r in regs:
                if r.faculty_id is not None:
                    lab_faculty_id = r.faculty_id
                    break

            # Check weekly load cap (lab counts as 2 slots per batch, 4 total per session)
            fac = regs[0].faculty if hasattr(regs[0], "faculty") else None
            max_load = getattr(fac, "max_weekly_load", 99) if fac else 99

            sessions_placed = 0
            day_order = list(range(num_days))
            rng.shuffle(day_order)

            for _ in range(req_lab):
                placed = False
                for d in day_order:
                    for ss in valid_lab_starts:
                        s2 = ss + 1
                        # Check both consecutive slots free in this section
                        if sec_busy.get((d, ss)) or sec_busy.get((d, s2)):
                            continue
                        # Check faculty not busy (both slots)
                        if lab_faculty_id:
                            if faculty_busy.get((lab_faculty_id, d, ss)) or \
                               faculty_busy.get((lab_faculty_id, d, s2)):
                                continue
                            # Check weekly load (2 extra slots for this session)
                            if max_load > 0 and faculty_weekly_used[lab_faculty_id] + 2 > max_load:
                                continue
                        # Check lab rooms free
                        if lab_room_busy.get((lab_rooms[0], d, ss)) or \
                           lab_room_busy.get((lab_rooms[0], d, s2)) or \
                           lab_room_busy.get((lab_rooms[1], d, ss)) or \
                           lab_room_busy.get((lab_rooms[1], d, s2)):
                            continue

                        # Place it!
                        for slot_idx, slot_label in [(ss, teaching_slots[ss]), (s2, teaching_slots[s2])]:
                            for bi, blabel in enumerate(batch_labels):
                                generated_entries.append(TimetableEntry(
                                    batch_id=batch_id,
                                    day=DAYS[d],
                                    time_slot=slot_label,
                                    section=section,
                                    lab_batch=blabel,
                                    room=lab_rooms[bi],
                                    faculty_id=lab_faculty_id,
                                    subject_id=subj_id,
                                ))
                            sec_busy[(d, slot_idx)] = True
                            lab_room_busy[(lab_rooms[0], d, slot_idx)] = True
                            lab_room_busy[(lab_rooms[1], d, slot_idx)] = True

                        if lab_faculty_id:
                            faculty_busy[(lab_faculty_id, d, ss)] = True
                            faculty_busy[(lab_faculty_id, d, s2)] = True
                            faculty_weekly_used[lab_faculty_id] += 2

                        sessions_placed += 1
                        placed = True
                        break
                    if placed:
                        break

                if not placed:
                    rep = regs[0]
                    unassigned.append({
                        "faculty": getattr(rep.faculty, "name", "Unassigned") if rep.faculty else "Unassigned",
                        "subject": subject.name,
                        "section": section,
                        "remaining_slots": req_lab - sessions_placed,
                        "type": "lab",
                    })
                    break

        # ── 2. Schedule THEORY lectures ─────────────────────────────────────
        for subj_id in subj_ids:
            regs = subj_regs[subj_id]
            subject = regs[0].subject
            req_theory = required_theory_slots(subject)
            if req_theory == 0:
                continue

            # Pick faculty
            theory_faculty_id = None
            for r in regs:
                if r.faculty_id is not None:
                    theory_faculty_id = r.faculty_id
                    break

            fac = regs[0].faculty if hasattr(regs[0], "faculty") else None
            max_load = getattr(fac, "max_weekly_load", 99) if fac else 99

            placed_count = 0
            # Track which days already have this subject (max 1 per day)
            used_days = set()
            day_order = list(range(num_days))
            rng.shuffle(day_order)

            for _ in range(req_theory):
                placed = False
                for d in day_order:
                    if d in used_days:
                        continue
                    slot_order = list(range(num_slots))
                    rng.shuffle(slot_order)
                    for s in slot_order:
                        if sec_busy.get((d, s)):
                            continue
                        if theory_faculty_id:
                            if faculty_busy.get((theory_faculty_id, d, s)):
                                continue
                            if max_load > 0 and faculty_weekly_used[theory_faculty_id] + 1 > max_load:
                                continue

                        # Place it
                        generated_entries.append(TimetableEntry(
                            batch_id=batch_id,
                            day=DAYS[d],
                            time_slot=teaching_slots[s],
                            section=section,
                            lab_batch=None,
                            room=default_room,
                            faculty_id=theory_faculty_id,
                            subject_id=subj_id,
                        ))
                        sec_busy[(d, s)] = True
                        if theory_faculty_id:
                            faculty_busy[(theory_faculty_id, d, s)] = True
                            faculty_weekly_used[theory_faculty_id] += 1
                        used_days.add(d)
                        placed_count += 1
                        placed = True
                        break
                    if placed:
                        break

                if not placed:
                    rep = regs[0]
                    unassigned.append({
                        "faculty": getattr(rep.faculty, "name", "Unassigned") if rep.faculty else "Unassigned",
                        "subject": subject.name,
                        "section": section,
                        "remaining_slots": req_theory - placed_count,
                        "type": "theory",
                    })
                    break

    print(f"[Greedy] Generated {len(generated_entries)} entries, {len(unassigned)} unassigned.")
    return generated_entries, unassigned


def build_timetable_all_sections(all_registrations_by_section, batch_id, room_config=None):
    """Build timetables for ALL sections in a single CP-SAT model.

    This prevents cross-section clashes:
    - Faculty cannot teach two sections at the same (day, slot).
    - Only one section can use lab rooms at any given (day, start_slot).
    - Faculty weekly load is enforced globally across all sections.
    """
    generated_entries = []
    unassigned = []

    if room_config is None:
        room_config = {"default_room": None, "lab_room_1": "Lab-131", "lab_room_2": "Lab-132"}

    lab_rooms = [room_config.get("lab_room_1", "Lab-131"), room_config.get("lab_room_2", "Lab-132")]
    default_room = room_config.get("default_room")

    if not all_registrations_by_section:
        return generated_entries, unassigned

    # Lazy import: ortools is not available on Vercel; fall back to greedy scheduler
    try:
        from ortools.sat.python import cp_model
        _USE_ORTOOLS = True
    except ImportError:
        _USE_ORTOOLS = False
        print("WARNING: OR-Tools not available. Using greedy fallback scheduler.")

    if not _USE_ORTOOLS:
        return _build_timetable_greedy(
            all_registrations_by_section, batch_id, room_config
        )

    num_sections = len(all_registrations_by_section)
    print(f"\n*** SOLVER: Building unified model for {num_sections} section(s) ***")

    model = cp_model.CpModel()
    semester = all_registrations_by_section[0][0].subject.semester
    slot_template, teaching_slots = get_slot_template(semester)

    num_days = len(DAYS)
    num_slots = len(teaching_slots)
    b_count = 2
    batches = list(range(b_count))
    batch_labels = ["A1", "A2"]

    valid_lab_starts = []
    for index in range(len(slot_template) - 1):
        if slot_template[index]["is_break"] or slot_template[index + 1]["is_break"]:
            continue
        v1 = slot_template[index]["value"]
        v2 = slot_template[index + 1]["value"]
        s1 = teaching_slots.index(v1)
        s2 = teaching_slots.index(v2)
        if s2 == s1 + 1:
            valid_lab_starts.append(s1)

    # ── GLOBAL tracking vars ──
    global_faculty_slot_vars = defaultdict(list)
    global_room_slot_vars = defaultdict(list) # (room, d, s)

    # ── Per-section variable creation ──
    section_data = []
    for sec_idx, registrations in enumerate(all_registrations_by_section):
        section = registrations[0].preferred_section if registrations else f"S{sec_idx}"
        unique_subjects = {reg.subject_id: reg.subject for reg in registrations}

        subj_lab_vars = {}
        subj_theory_vars = {}
        reg_lab_vars = {}
        reg_theory_vars = {}

        for subj_id, subject in unique_subjects.items():
            req_lab = required_lab_sessions(subject)
            for d in range(num_days):
                for s in range(num_slots):
                    v = model.NewBoolVar(f"st_{sec_idx}_{subj_id}_{d}_{s}")
                    subj_theory_vars[(subj_id, d, s)] = v
                if req_lab > 0:
                    for b_idx in batches:
                        for start_s in valid_lab_starts:
                            v = model.NewBoolVar(f"sl_{sec_idx}_{subj_id}_{b_idx}_{d}_{start_s}")
                            subj_lab_vars[(subj_id, b_idx, d, start_s)] = v

        reg_active_vars = {}
        for reg_idx, reg in enumerate(registrations):
            reg_active_vars[reg_idx] = model.NewBoolVar(f"ra_{sec_idx}_{reg_idx}")
            subj_id = reg.subject_id
            for d in range(num_days):
                for s in range(num_slots):
                    v = model.NewBoolVar(f"rt_{sec_idx}_{reg_idx}_{d}_{s}")
                    reg_theory_vars[(reg_idx, d, s)] = v
                    if reg.faculty_id is not None:
                        global_faculty_slot_vars[(reg.faculty_id, d, s)].append(v)
                for b_idx in batches:
                    for start_s in valid_lab_starts:
                        v = model.NewBoolVar(f"rl_{sec_idx}_{reg_idx}_{b_idx}_{d}_{start_s}")
                        reg_lab_vars[(reg_idx, b_idx, d, start_s)] = v
                        if reg.faculty_id is not None:
                            global_faculty_slot_vars[(reg.faculty_id, d, start_s)].append(v)
                            global_faculty_slot_vars[(reg.faculty_id, d, start_s + 1)].append(v)
                        
                        # Room tracking
                        room_name = lab_rooms[b_idx]
                        global_room_slot_vars[(room_name, d, start_s)].append(v)
                        global_room_slot_vars[(room_name, d, start_s + 1)].append(v)

        section_data.append({
            "sec_idx": sec_idx, "section": section,
            "registrations": registrations, "unique_subjects": unique_subjects,
            "subj_lab_vars": subj_lab_vars, "subj_theory_vars": subj_theory_vars,
            "reg_lab_vars": reg_lab_vars, "reg_theory_vars": reg_theory_vars,
            "reg_active_vars": reg_active_vars,
        })

    # ── Per-section constraints (same rules as before, but scoped) ──
    objective_terms = []
    for sd in section_data:
        si = sd["sec_idx"]
        regs = sd["registrations"]
        usubs = sd["unique_subjects"]
        stv = sd["subj_theory_vars"]
        slv = sd["subj_lab_vars"]
        rtv = sd["reg_theory_vars"]
        rlv = sd["reg_lab_vars"]
        rav = sd["reg_active_vars"]

        for subj_id, subject in usubs.items():
            req_theory = required_theory_slots(subject)
            req_lab = required_lab_sessions(subject)

            all_st = [stv[(subj_id, d, s)] for d in range(num_days) for s in range(num_slots)]
            model.Add(sum(all_st) <= req_theory)
            objective_terms.append(50000 * sum(all_st))

            if req_lab > 0:
                for b_idx in batches:
                    all_sl = [slv[(subj_id, b_idx, d, ss)] for d in range(num_days) for ss in valid_lab_starts]
                    model.Add(sum(all_sl) <= req_lab)
                    objective_terms.append(1000000 * sum(all_sl))
                for d in range(num_days):
                    for ss in valid_lab_starts:
                        model.AddImplication(
                            slv[(subj_id, 0, d, ss)],
                            slv[(subj_id, 1, d, ss)].Not()
                        )

            for d in range(num_days):
                d_st = [stv[(subj_id, d, s)] for s in range(num_slots)]
                model.AddAtMostOne(d_st)

                if req_lab > 0:
                    for b_idx in batches:
                        d_sl = [slv[(subj_id, b_idx, d, ss)] for ss in valid_lab_starts]
                        model.AddAtMostOne(d_sl)

                    has_t = model.NewBoolVar(f"ht_{si}_{subj_id}_{d}")
                    has_l = model.NewBoolVar(f"hl_{si}_{subj_id}_{d}")
                    model.Add(sum(d_st) > 0).OnlyEnforceIf(has_t)
                    model.Add(sum(d_st) == 0).OnlyEnforceIf(has_t.Not())
                    all_d_sl = [slv[(subj_id, bi, d, ss)] for bi in batches for ss in valid_lab_starts]
                    model.Add(sum(all_d_sl) > 0).OnlyEnforceIf(has_l)
                    model.Add(sum(all_d_sl) == 0).OnlyEnforceIf(has_l.Not())
                    both = model.NewBoolVar(f"bo_{si}_{subj_id}_{d}")
                    model.AddImplication(both, has_t)
                    model.AddImplication(both, has_l)
                    model.AddBoolOr([has_t.Not(), has_l.Not(), both])
                    objective_terms.append(-500 * both)

        # Within-section: spread lab sessions evenly across days (max 1 per day if total <= num_days)
        total_section_labs = sum(required_lab_sessions(subject) for subject in usubs.values())
        max_labs_per_day = math.ceil(total_section_labs / num_days) if num_days > 0 else 1
        for d in range(num_days):
            daily_b0_labs = []
            for ss in valid_lab_starts:
                for subj_id in usubs:
                    if (subj_id, 0, d, ss) in slv:
                        daily_b0_labs.append(slv[(subj_id, 0, d, ss)])
            if daily_b0_labs:
                model.Add(sum(daily_b0_labs) <= max_labs_per_day)

        # Within-section: at most one thing per slot per batch
        for d in range(num_days):
            for s in range(num_slots):
                for b_idx in batches:
                    slot_vars = []
                    for subj_id in usubs:
                        slot_vars.append(stv[(subj_id, d, s)])
                        for ss in valid_lab_starts:
                            if s in (ss, ss + 1) and (subj_id, b_idx, d, ss) in slv:
                                slot_vars.append(slv[(subj_id, b_idx, d, ss)])
                    model.AddAtMostOne(slot_vars)

            for ss in valid_lab_starts:
                b0 = [slv[(sid, 0, d, ss)] for sid in usubs if (sid, 0, d, ss) in slv]
                b1 = [slv[(sid, 1, d, ss)] for sid in usubs if (sid, 1, d, ss) in slv]
                if b0 or b1:
                    model.Add(sum(b0) == sum(b1))

        # Within-section: one faculty per subject
        for subj_id in usubs:
            rfs = [i for i, r in enumerate(regs) if r.subject_id == subj_id]
            model.AddAtMostOne([rav[r] for r in rfs])
            for d in range(num_days):
                for s in range(num_slots):
                    fv = [rtv[(r, d, s)] for r in rfs]
                    model.Add(sum(fv) == stv[(subj_id, d, s)])
                    for r in rfs:
                        model.AddImplication(rtv[(r, d, s)], rav[r])
                for b_idx in batches:
                    for ss in valid_lab_starts:
                        if (subj_id, b_idx, d, ss) in slv:
                            fv = [rlv[(r, b_idx, d, ss)] for r in rfs]
                            model.Add(sum(fv) == slv[(subj_id, b_idx, d, ss)])
                            for r in rfs:
                                model.AddImplication(rlv[(r, b_idx, d, ss)], rav[r])

    # ══════════════════════════════════════════════════════
    # CROSS-SECTION CONSTRAINTS (the fix for the clashing)
    # ══════════════════════════════════════════════════════

    # 1. Faculty can teach at most ONE thing per (day, slot) across ALL sections
    for vlist in global_faculty_slot_vars.values():
        model.AddAtMostOne(vlist)

    # 1b. Room can be used by at most ONE section/batch per (day, slot)
    for vlist in global_room_slot_vars.values():
        model.AddAtMostOne(vlist)

    # 2. Only ONE section can use lab rooms at a given (day, start_slot)
    #    (we have 2 lab rooms and each section needs both for its 2 batches)
    for d in range(num_days):
        for ss in valid_lab_starts:
            sec_lab_indicators = []
            for sd in section_data:
                slv = sd["subj_lab_vars"]
                usubs = sd["unique_subjects"]
                labs_at_slot = []
                for sid in usubs:
                    for bi in batches:
                        if (sid, bi, d, ss) in slv:
                            labs_at_slot.append(slv[(sid, bi, d, ss)])
                if labs_at_slot:
                    ind = model.NewBoolVar(f"sli_{sd['sec_idx']}_{d}_{ss}")
                    model.Add(sum(labs_at_slot) > 0).OnlyEnforceIf(ind)
                    model.Add(sum(labs_at_slot) == 0).OnlyEnforceIf(ind.Not())
                    sec_lab_indicators.append(ind)
            if len(sec_lab_indicators) > 1:
                model.AddAtMostOne(sec_lab_indicators)

    # 3. Global faculty weekly load & CROSS-SEMESTER CLASH PREVENTION
    faculty_map = {}
    faculty_all_vars = defaultdict(list)
    for sd in section_data:
        for reg in sd["registrations"]:
            if reg.faculty_id is not None:
                faculty_map[reg.faculty_id] = reg.faculty
    
    unique_fids = set(faculty_map.keys())
    existing_load = defaultdict(int)
    
    if unique_fids:
        # Get ALL timetable entries from ALL previous batches (excluding the one currently being built)
        existing_entries = TimetableEntry.query.filter(
            TimetableEntry.faculty_id.in_(unique_fids),
            TimetableEntry.batch_id != batch_id
        ).all()
        
        if existing_entries:
            for entry in existing_entries:
                existing_load[entry.faculty_id] += 1
                if entry.day in DAYS and entry.time_slot in teaching_slots:
                    d = DAYS.index(entry.day)
                    s = teaching_slots.index(entry.time_slot)
                    if (entry.faculty_id, d, s) in global_faculty_slot_vars:
                        # Block this specific day/slot for this faculty to prevent cross-semester clashes
                        for v in global_faculty_slot_vars[(entry.faculty_id, d, s)]:
                            model.Add(v == 0)

    for (fid, d, s), vlist in global_faculty_slot_vars.items():
        faculty_all_vars[fid].extend(vlist)
        
    for fid, vlist in faculty_all_vars.items():
        fac = faculty_map.get(fid)
        if fac and getattr(fac, 'max_weekly_load', 0) > 0:
            available_load = max(0, fac.max_weekly_load - existing_load[fid])
            model.Add(sum(vlist) <= available_load)

    # 4. Schedule diversity: penalize when two sections have the same
    #    subject at the same (day, slot) — forces genuinely different timetables
    if len(section_data) > 1:
        for i in range(len(section_data)):
            for j in range(i + 1, len(section_data)):
                sd_a = section_data[i]
                sd_b = section_data[j]
                # Find subjects common to both sections
                common_subs = set(sd_a["unique_subjects"]) & set(sd_b["unique_subjects"])
                for subj_id in common_subs:
                    for d in range(num_days):
                        for s in range(num_slots):
                            va = sd_a["subj_theory_vars"][(subj_id, d, s)]
                            vb = sd_b["subj_theory_vars"][(subj_id, d, s)]
                            # both_on = 1 when BOTH sections teach this subject
                            # at the same (day, slot)
                            both_on = model.NewBoolVar(
                                f"clash_{i}_{j}_{subj_id}_{d}_{s}"
                            )
                            model.AddImplication(both_on, va)
                            model.AddImplication(both_on, vb)
                            model.AddBoolOr([va.Not(), vb.Not(), both_on])
                            # Heavy penalty to push sections apart
                            objective_terms.append(-10000 * both_on)

    # 5. Daily shuffling: penalize a subject for occupying the SAME slot
    #    on multiple days (prevents Mon slot3 = Tue slot3 = Wed slot3 pattern)
    for sd in section_data:
        si = sd["sec_idx"]
        stv = sd["subj_theory_vars"]
        usubs = sd["unique_subjects"]
        for subj_id in usubs:
            for s in range(num_slots):
                # Count how many days this subject sits in this slot
                days_in_slot = [stv[(subj_id, d, s)] for d in range(num_days)]
                if len(days_in_slot) > 1:
                    # Create indicator: did subject appear in this slot on 2+ days?
                    repeat = model.NewBoolVar(f"rp_{si}_{subj_id}_{s}")
                    model.Add(sum(days_in_slot) >= 2).OnlyEnforceIf(repeat)
                    model.Add(sum(days_in_slot) <= 1).OnlyEnforceIf(repeat.Not())
                    objective_terms.append(-3000 * repeat)

        # 5b. Compactness: Penalize gaps between classes
        for d in range(num_days):
            for s in range(num_slots - 1):
                # If there's a class at s and a class at s+2, but NOT at s+1, penalize
                # Actually, simpler: penalize "active" slots that are far apart
                pass # Complexity of gap-minimization in CP-SAT can be high, 
                     # let's use a simpler "early slots" preference for now
            
            for s in range(num_slots):
                # Prefer earlier slots slightly to keep it compact from the top
                weight = (num_slots - s) * 10 
                for subj_id in usubs:
                    objective_terms.append(weight * stv[(subj_id, d, s)])

    # 6. Randomization: add small random bonus per (subject, day, slot)
    #    so the solver breaks ties differently each time → different timetable
    rng = random.Random()  # new seed each invocation
    for sd in section_data:
        stv = sd["subj_theory_vars"]
        usubs = sd["unique_subjects"]
        for subj_id in usubs:
            for d in range(num_days):
                for s in range(num_slots):
                    # Small random weight (1-100) won't override real constraints
                    # but breaks the solver's tie-breaking symmetry
                    objective_terms.append(rng.randint(1, 100) * stv[(subj_id, d, s)])

    # ── Solve ──
    model.Maximize(sum(objective_terms))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 20.0 # Reduced from 120s for better responsiveness
    solver.parameters.num_search_workers = 8 # Increased workers for faster search
    solver.parameters.random_seed = random.randint(0, 2**31 - 1)
    status = solver.Solve(model)

    # ── Extract results per section ──
    global_occupied_faculty = defaultdict(set)
    global_occupied_rooms = defaultdict(set)
    
    for sd in section_data:
        section = sd["section"]
        regs = sd["registrations"]
        usubs = sd["unique_subjects"]
        stv = sd["subj_theory_vars"]
        slv = sd["subj_lab_vars"]
        rtv = sd["reg_theory_vars"]
        rlv = sd["reg_lab_vars"]
        rav = sd["reg_active_vars"]

        occupied_slots = defaultdict(list)
        section_entries = []

        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            for d in range(num_days):
                for s in range(num_slots):
                    for subj_id in usubs:
                        if solver.Value(stv[(subj_id, d, s)]):
                            for bi in batches:
                                occupied_slots[bi].append((d, s))
                            rfs = [i for i, r in enumerate(regs) if r.subject_id == subj_id]
                            for r in rfs:
                                if solver.Value(rtv[(r, d, s)]):
                                    section_entries.append(TimetableEntry(
                                        batch_id=batch_id, day=DAYS[d],
                                        time_slot=teaching_slots[s],
                                        section=section, faculty_id=regs[r].faculty_id,
                                        subject_id=subj_id,
                                        room=default_room
                                    ))
                                    break
                                    
            for d in range(num_days):
                for ss in valid_lab_starts:
                    ab0 = ab1 = None
                    for sid in usubs:
                        if (sid, 0, d, ss) in slv and solver.Value(slv[(sid, 0, d, ss)]):
                            ab0 = sid
                            break
                    for sid in usubs:
                        if (sid, 1, d, ss) in slv and solver.Value(slv[(sid, 1, d, ss)]):
                            ab1 = sid
                            break
                    if ab0 and ab1:
                        for bi in batches:
                            occupied_slots[bi].append((d, ss))
                            occupied_slots[bi].append((d, ss + 1))
                        rooms = lab_rooms
                        for bi, sid in enumerate([ab0, ab1]):
                            rfs = [i for i, r in enumerate(regs) if r.subject_id == sid]
                            fac = None
                            for r in rfs:
                                if solver.Value(rlv[(r, bi, d, ss)]):
                                    fac = regs[r].faculty_id
                                    break
                            section_entries.append(TimetableEntry(
                                batch_id=batch_id, day=DAYS[d],
                                time_slot=teaching_slots[ss],
                                section=section, lab_batch=batch_labels[bi],
                                room=rooms[bi], faculty_id=fac, subject_id=sid
                            ))
                            section_entries.append(TimetableEntry(
                                batch_id=batch_id, day=DAYS[d],
                                time_slot=teaching_slots[ss + 1],
                                section=section, lab_batch=batch_labels[bi],
                                room=rooms[bi], faculty_id=fac, subject_id=sid
                            ))

            # Update global occupied tracking for fallback
            for entry in section_entries:
                if entry.faculty_id:
                    global_occupied_faculty[(DAYS.index(entry.day), teaching_slots.index(entry.time_slot))].add(entry.faculty_id)
                if entry.room:
                    global_occupied_rooms[(DAYS.index(entry.day), teaching_slots.index(entry.time_slot))].add(entry.room)

            # Fallback for any unassigned theory slots
            subj_day_count = defaultdict(int)
            for e in section_entries:
                if e.lab_batch is None:
                    subj_day_count[(e.subject_id, DAYS.index(e.day))] += 1

            for subj_id, subject in usubs.items():
                req_theory = required_theory_slots(subject)
                req_lab = required_lab_sessions(subject)
                assigned_t = sum(1 for e in section_entries if e.subject_id == subj_id and e.lab_batch is None)
                b0_labs = sum(1 for e in section_entries if e.subject_id == subj_id and e.lab_batch == batch_labels[0]) // 2
                b1_labs = sum(1 for e in section_entries if e.subject_id == subj_id and e.lab_batch == batch_labels[1]) // 2

                missing_t = req_theory - assigned_t
                if missing_t > 0:
                    for _ in range(missing_t):
                        placed = False
                        for d in range(num_days):
                            if subj_day_count[(subj_id, d)] > 0:
                                continue
                            for s in range(num_slots):
                                if (d, s) not in occupied_slots[0] and (d, s) not in occupied_slots[1]:
                                    rfs = [i2 for i2, r in enumerate(regs) if r.subject_id == subj_id]
                                    active_fac = None
                                    for r in rfs:
                                        if solver.Value(rav[r]):
                                            active_fac = regs[r].faculty_id
                                            break
                                    if not active_fac and rfs:
                                        active_fac = regs[rfs[0]].faculty_id
                                    
                                    if active_fac and active_fac in global_occupied_faculty[(d, s)]:
                                        continue
                                    
                                    occupied_slots[0].append((d, s))
                                    occupied_slots[1].append((d, s))
                                    if active_fac:
                                        global_occupied_faculty[(d, s)].add(active_fac)
                                    
                                    subj_day_count[(subj_id, d)] += 1
                                    section_entries.append(TimetableEntry(
                                        batch_id=batch_id, day=DAYS[d],
                                        time_slot=teaching_slots[s],
                                        section=section, faculty_id=active_fac,
                                        subject_id=subj_id,
                                        room=default_room
                                    ))
                                    placed = True
                                    break
                            if placed:
                                break
                        if not placed:
                            rep = next((r for r in regs if r.subject_id == subj_id), None)
                            if rep:
                                unassigned.append({
                                    "faculty": getattr(rep.faculty, "name", "Unassigned") if rep.faculty else "Unassigned",
                                    "subject": subject.name, "section": section,
                                    "remaining_slots": 1, "type": "theory"
                                })

                missing_l_b0 = req_lab - b0_labs
                missing_l_b1 = req_lab - b1_labs
                if missing_l_b0 > 0 or missing_l_b1 > 0:
                    rep = next((r for r in regs if r.subject_id == subj_id), None)
                    if rep:
                        unassigned.append({
                            "faculty": getattr(rep.faculty, "name", "Unassigned") if rep.faculty else "Unassigned",
                            "subject": subject.name, "section": section,
                            "remaining_slots": max(missing_l_b0, missing_l_b1), "type": "lab"
                        })
        else:
            for reg in regs:
                unassigned.append({
                    "faculty": getattr(reg.faculty, "name", "Unassigned") if reg.faculty else "Unassigned",
                    "subject": reg.subject.name,
                    "section": reg.preferred_section,
                    "remaining_slots": required_theory_slots(reg.subject) + required_lab_sessions(reg.subject),
                    "type": "theory/lab"
                })

        generated_entries.extend(section_entries)

    return generated_entries, unassigned


def create_timetable_batch(all_registrations_by_section, semester, room_config=None):
    max_retries = 2 # Reduced retries for performance
    last_unassigned = []
    last_validation_errors = []
    _, teaching_slots = get_slot_template(semester)

    for attempt in range(max_retries):
        try:
            print(f"\n--- Generation Attempt {attempt + 1} ---")
            batch = TimetableBatch(
                name=f"Sem {semester} Timetable (Attempt {attempt+1})",
                semester=semester,
            )
            db.session.add(batch)
            db.session.flush()

            all_entries, all_unassigned = build_timetable_all_sections(
                all_registrations_by_section, batch.id, room_config=room_config
            )

            is_valid, validation_errors = validate_timetable(all_entries, teaching_slots)

            if is_valid:
                db.session.add_all(all_entries)
                db.session.commit()
                print(f"Attempt {attempt + 1} succeeded with {len(all_entries)} entries.")
                return batch, all_entries, all_unassigned
            else:
                print(f"Attempt {attempt + 1} failed validation: {validation_errors}")
                last_validation_errors = validation_errors
                last_unassigned = all_unassigned
                db.session.rollback()
        except Exception as e:
            import traceback
            print(f"Error during attempt {attempt + 1}: {str(e)}")
            traceback.print_exc()
            db.session.rollback()

    # Final attempt fallback — commit whatever we get even if not perfectly valid
    print("\n--- Final Fallback Attempt ---")
    try:
        batch = TimetableBatch(
            name=f"Sem {semester} Timetable (Final)",
            semester=semester,
        )
        db.session.add(batch)
        db.session.flush()
        all_entries, all_unassigned = build_timetable_all_sections(
            all_registrations_by_section, batch.id, room_config=room_config
        )
        db.session.add_all(all_entries)
        db.session.commit()
        print(f"Final fallback committed {len(all_entries)} entries.")
        return batch, all_entries, all_unassigned
    except Exception as e:
        import traceback
        print(f"Final fallback also failed: {str(e)}")
        traceback.print_exc()
        db.session.rollback()
        raise RuntimeError(
            f"Timetable generation failed after all attempts. "
            f"Last validation errors: {last_validation_errors}. "
            f"Root cause: {str(e)}"
        ) from e
