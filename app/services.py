import math
import random
from collections import defaultdict
from ortools.sat.python import cp_model

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
            "label": f"{start_time}<br>-<br>{end_time}",
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


def build_timetable(registrations, batch_id):
    generated_entries = []
    unassigned = []

    if not registrations:
        return generated_entries, unassigned

    model = cp_model.CpModel()
    semester = registrations[0].subject.semester
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

    unique_subjects = {reg.subject_id: reg.subject for reg in registrations}
    section = registrations[0].preferred_section if registrations else "A"
    
    subj_lab_vars = {}
    subj_theory_vars = {}
    reg_lab_vars = {}
    reg_theory_vars = {}
    faculty_slot_vars = defaultdict(list)
    
    for subj_id, subject in unique_subjects.items():
        req_theory = required_theory_slots(subject)
        req_lab = required_lab_sessions(subject)
        
        for d in range(num_days):
            for s in range(num_slots):
                v = model.NewBoolVar(f"st_{subj_id}_{d}_{s}")
                subj_theory_vars[(subj_id, d, s)] = v
                
            if req_lab > 0:
                for b_idx in batches:
                    for start_s in valid_lab_starts:
                        v = model.NewBoolVar(f"sl_{subj_id}_{b_idx}_{d}_{start_s}")
                        subj_lab_vars[(subj_id, b_idx, d, start_s)] = v

    reg_active_vars = {}
    for reg_idx, reg in enumerate(registrations):
        reg_active_vars[reg_idx] = model.NewBoolVar(f"reg_active_{reg_idx}")
        subj_id = reg.subject_id
        for d in range(num_days):
            for s in range(num_slots):
                v = model.NewBoolVar(f"rt_{reg_idx}_{d}_{s}")
                reg_theory_vars[(reg_idx, d, s)] = v
                if reg.faculty_id is not None:
                    faculty_slot_vars[(reg.faculty_id, d, s)].append(v)
            
            for b_idx in batches:
                for start_s in valid_lab_starts:
                    v = model.NewBoolVar(f"rl_{reg_idx}_{b_idx}_{d}_{start_s}")
                    reg_lab_vars[(reg_idx, b_idx, d, start_s)] = v
                    if reg.faculty_id is not None:
                        faculty_slot_vars[(reg.faculty_id, d, start_s)].append(v)
                        faculty_slot_vars[(reg.faculty_id, d, start_s + 1)].append(v)

    objective_terms = []
    
    for subj_id, subject in unique_subjects.items():
        req_theory = required_theory_slots(subject)
        req_lab = required_lab_sessions(subject)
        
        all_st = [subj_theory_vars[(subj_id, d, s)] for d in range(num_days) for s in range(num_slots)]
        model.Add(sum(all_st) <= req_theory)
        assigned_t = sum(all_st)
        # High weight so solver strongly prioritises filling all theory slots
        weight = 50000
        objective_terms.append(weight * assigned_t)
        
        if req_lab > 0:
            for b_idx in batches:
                all_sl = [subj_lab_vars[(subj_id, b_idx, d, start_s)] for d in range(num_days) for start_s in valid_lab_starts]
                model.Add(sum(all_sl) <= req_lab)
                objective_terms.append(1000000 * sum(all_sl))
                
            for d in range(num_days):
                for start_s in valid_lab_starts:
                    model.AddImplication(
                        subj_lab_vars[(subj_id, 0, d, start_s)],
                        subj_lab_vars[(subj_id, 1, d, start_s)].Not()
                    )
                    
        for d in range(num_days):
            d_st = [subj_theory_vars[(subj_id, d, s)] for s in range(num_slots)]
            model.AddAtMostOne(d_st)
            
            if req_lab > 0:
                for b_idx in batches:
                    d_sl = [subj_lab_vars[(subj_id, b_idx, d, start_s)] for start_s in valid_lab_starts]
                    model.AddAtMostOne(d_sl)
                
                has_t = model.NewBoolVar(f"has_t_{subj_id}_{d}")
                has_l = model.NewBoolVar(f"has_l_{subj_id}_{d}")
                model.Add(sum(d_st) > 0).OnlyEnforceIf(has_t)
                model.Add(sum(d_st) == 0).OnlyEnforceIf(has_t.Not())
                all_d_sl = [subj_lab_vars[(subj_id, b_idx, d, start_s)] for b_idx in batches for start_s in valid_lab_starts]
                model.Add(sum(all_d_sl) > 0).OnlyEnforceIf(has_l)
                model.Add(sum(all_d_sl) == 0).OnlyEnforceIf(has_l.Not())
                
                both_var = model.NewBoolVar(f"both_{subj_id}_{d}")
                model.AddImplication(both_var, has_t)
                model.AddImplication(both_var, has_l)
                model.AddBoolOr([has_t.Not(), has_l.Not(), both_var])
                # Strongly discourage theory+lab on same day so solver
                # keeps days clean and avoids the 5th-slot-missing problem
                objective_terms.append(-500 * both_var)

    for d in range(num_days):
        for s in range(num_slots):
            for b_idx in batches:
                slot_vars = []
                for subj_id in unique_subjects.keys():
                    slot_vars.append(subj_theory_vars[(subj_id, d, s)])
                    for start_s in valid_lab_starts:
                        if s == start_s or s == start_s + 1:
                            if (subj_id, b_idx, d, start_s) in subj_lab_vars:
                                slot_vars.append(subj_lab_vars[(subj_id, b_idx, d, start_s)])
                model.AddAtMostOne(slot_vars)

        for start_s in valid_lab_starts:
            b0_labs = [subj_lab_vars[(subj_id, 0, d, start_s)] for subj_id in unique_subjects.keys() if (subj_id, 0, d, start_s) in subj_lab_vars]
            b1_labs = [subj_lab_vars[(subj_id, 1, d, start_s)] for subj_id in unique_subjects.keys() if (subj_id, 1, d, start_s) in subj_lab_vars]
            if b0_labs or b1_labs:
                model.Add(sum(b0_labs) == sum(b1_labs))

    for subj_id in unique_subjects.keys():
        regs_for_subj = [idx for idx, r in enumerate(registrations) if r.subject_id == subj_id]
        
        # New constraint: at most one faculty assigned per subject
        model.AddAtMostOne([reg_active_vars[r] for r in regs_for_subj])
        
        for d in range(num_days):
            for s in range(num_slots):
                fac_vars = [reg_theory_vars[(r, d, s)] for r in regs_for_subj]
                model.Add(sum(fac_vars) == subj_theory_vars[(subj_id, d, s)])
                # Link assignment to active faculty var
                for r in regs_for_subj:
                    model.AddImplication(reg_theory_vars[(r, d, s)], reg_active_vars[r])
                
            for b_idx in batches:
                for start_s in valid_lab_starts:
                    if (subj_id, b_idx, d, start_s) in subj_lab_vars:
                        fac_vars = [reg_lab_vars[(r, b_idx, d, start_s)] for r in regs_for_subj]
                        model.Add(sum(fac_vars) == subj_lab_vars[(subj_id, b_idx, d, start_s)])
                        for r in regs_for_subj:
                            model.AddImplication(reg_lab_vars[(r, b_idx, d, start_s)], reg_active_vars[r])

    for vars_list in faculty_slot_vars.values():
        model.AddAtMostOne(vars_list)
        
    faculty_map = {reg.faculty_id: reg.faculty for reg in registrations if reg.faculty_id is not None}
    faculty_all_vars = defaultdict(list)
    for (fid, d, s), vars_list in faculty_slot_vars.items():
        faculty_all_vars[fid].extend(vars_list)
        
    for fid, vars_list in faculty_all_vars.items():
        faculty = faculty_map.get(fid)
        if faculty and getattr(faculty, 'max_weekly_load', 0) > 0:
            model.Add(sum(vars_list) <= faculty.max_weekly_load)

    model.Maximize(sum(objective_terms))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 60.0
    solver.parameters.num_search_workers = 4  # parallel search
    status = solver.Solve(model)
    
    occupied_slots = defaultdict(list)

    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        for d in range(num_days):
            for s in range(num_slots):
                for subj_id in unique_subjects.keys():
                    if solver.Value(subj_theory_vars[(subj_id, d, s)]):
                        for b_idx in batches:
                            occupied_slots[b_idx].append((d, s))
                        regs_for_subj = [idx for idx, r in enumerate(registrations) if r.subject_id == subj_id]
                        for r in regs_for_subj:
                            if solver.Value(reg_theory_vars[(r, d, s)]):
                                fac = registrations[r].faculty_id
                                generated_entries.append(TimetableEntry(
                                    batch_id=batch_id, day=DAYS[d], time_slot=teaching_slots[s],
                                    section=section, faculty_id=fac, subject_id=subj_id
                                ))
                                break
        
        for d in range(num_days):
            for start_s in valid_lab_starts:
                assigned_b0 = None
                assigned_b1 = None
                
                for subj_id in unique_subjects.keys():
                    if (subj_id, 0, d, start_s) in subj_lab_vars and solver.Value(subj_lab_vars[(subj_id, 0, d, start_s)]):
                        assigned_b0 = subj_id
                        break
                
                for subj_id in unique_subjects.keys():
                    if (subj_id, 1, d, start_s) in subj_lab_vars and solver.Value(subj_lab_vars[(subj_id, 1, d, start_s)]):
                        assigned_b1 = subj_id
                        break
                
                if assigned_b0 and assigned_b1:
                    occupied_slots[0].append((d, start_s))
                    occupied_slots[0].append((d, start_s + 1))
                    occupied_slots[1].append((d, start_s))
                    occupied_slots[1].append((d, start_s + 1))
                    
                    rooms = ["Lab-131", "Lab-132"]
                    for b_idx, s_id in enumerate([assigned_b0, assigned_b1]):
                        regs_for_subj = [idx for idx, r in enumerate(registrations) if r.subject_id == s_id]
                        fac = None
                        for r in regs_for_subj:
                            if solver.Value(reg_lab_vars[(r, b_idx, d, start_s)]):
                                fac = registrations[r].faculty_id
                                break
                                
                        generated_entries.append(TimetableEntry(
                            batch_id=batch_id, day=DAYS[d], time_slot=teaching_slots[start_s],
                            section=section, lab_batch=batch_labels[b_idx], room=rooms[b_idx],
                            faculty_id=fac, subject_id=s_id
                        ))
                        generated_entries.append(TimetableEntry(
                            batch_id=batch_id, day=DAYS[d], time_slot=teaching_slots[start_s + 1],
                            section=section, lab_batch=batch_labels[b_idx], room=rooms[b_idx],
                            faculty_id=fac, subject_id=s_id
                        ))

        # Track how many theory entries each subject already has per day (from solver output)
        subj_day_count = defaultdict(int)
        for e in generated_entries:
            if e.lab_batch is None:
                day_idx = DAYS.index(e.day)
                subj_day_count[(e.subject_id, day_idx)] += 1

        for subj_id, subject in unique_subjects.items():
            req_theory = required_theory_slots(subject)
            req_lab = required_lab_sessions(subject)
            
            assigned_t = sum(1 for e in generated_entries if e.subject_id == subj_id and e.lab_batch is None)
            b0_labs = sum(1 for e in generated_entries if e.subject_id == subj_id and e.lab_batch == batch_labels[0]) // 2
            b1_labs = sum(1 for e in generated_entries if e.subject_id == subj_id and e.lab_batch == batch_labels[1]) // 2
            
            missing_t = req_theory - assigned_t
            if missing_t > 0:
                for _ in range(missing_t):
                    placed = False

                    # Pass 1: prefer a fresh day (one-theory-per-day rule)
                    for d in range(num_days):
                        if subj_day_count[(subj_id, d)] > 0:
                            continue
                        for s in range(num_slots):
                            if (d, s) not in occupied_slots[0] and (d, s) not in occupied_slots[1]:
                                occupied_slots[0].append((d, s))
                                occupied_slots[1].append((d, s))
                                subj_day_count[(subj_id, d)] += 1
                                regs_for_subj = [idx2 for idx2, r in enumerate(registrations) if r.subject_id == subj_id]
                                active_fac = None
                                for r in regs_for_subj:
                                    if solver.Value(reg_active_vars[r]):
                                        active_fac = registrations[r].faculty_id
                                        break
                                if not active_fac and regs_for_subj:
                                    active_fac = registrations[regs_for_subj[0]].faculty_id
                                generated_entries.append(TimetableEntry(
                                    batch_id=batch_id, day=DAYS[d], time_slot=teaching_slots[s],
                                    section=section, faculty_id=active_fac, subject_id=subj_id
                                ))
                                placed = True
                                break
                        if placed:
                            break

                    if not placed:
                        rep_reg = next(reg for reg in registrations if reg.subject_id == subj_id)
                        unassigned.append({
                            "faculty": getattr(rep_reg.faculty, "name", "Unassigned") if rep_reg.faculty else "Unassigned",
                            "subject": subject.name, "section": section,
                            "remaining_slots": 1, "type": "theory"
                        })
            
            missing_l_b0 = req_lab - b0_labs
            missing_l_b1 = req_lab - b1_labs
            if missing_l_b0 > 0 or missing_l_b1 > 0:
                rep_reg = next(reg for reg in registrations if reg.subject_id == subj_id)
                unassigned.append({
                    "faculty": getattr(rep_reg.faculty, "name", "Unassigned") if rep_reg.faculty else "Unassigned",
                    "subject": subject.name, "section": section,
                    "remaining_slots": max(missing_l_b0, missing_l_b1), "type": "lab"
                })

    else:
        for reg in registrations:
            unassigned.append({
                    "faculty": getattr(reg.faculty, "name", "Unassigned") if reg.faculty else "Unassigned",
                    "subject": reg.subject.name,
                    "section": reg.preferred_section,
                    "remaining_slots": required_theory_slots(reg.subject) + required_lab_sessions(reg.subject),
                    "type": "theory/lab"
            })

    return generated_entries, unassigned

def create_timetable_batch(all_registrations_by_section, semester):
    batch = TimetableBatch(
        name=f"Semester {semester} Timetable",
        semester=semester,
    )
    db.session.add(batch)
    db.session.flush()

    all_entries = []
    all_unassigned = []
    
    for section_regs in all_registrations_by_section:
        if not section_regs:
            continue
        entries, unassigned = build_timetable(section_regs, batch.id)
        all_entries.extend(entries)
        all_unassigned.extend(unassigned)

    db.session.add_all(all_entries)
    db.session.commit()
    return batch, all_entries, all_unassigned
