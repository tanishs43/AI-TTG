from collections import defaultdict
from types import SimpleNamespace

from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

from .models import (
    Faculty,
    FacultyCapability,
    FacultySubjectRegistration,
    Subject,
    TimetableBatch,
    TimetableEntry,
    User,
    db,
)
from .services import DAYS, create_timetable_batch, get_timetable_columns


main = Blueprint("main", __name__)
SEMESTER_OPTIONS = list(range(1, 9))
MAX_WEEKLY_TEACHING_SLOTS = len(
    [column for column in get_timetable_columns(1) if column["type"] == "class"]
) * len(DAYS)


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    return User.query.get(user_id)


def faculty_timetable_rows(faculty_id, batch_id):
    if not batch_id:
        return [], defaultdict(dict)

    entries = (
        TimetableEntry.query.filter_by(faculty_id=faculty_id, batch_id=batch_id)
        .order_by(TimetableEntry.day, TimetableEntry.time_slot)
        .all()
    )
    timetable_map = defaultdict(dict)
    for entry in entries:
        timetable_map[entry.day][entry.time_slot] = entry
    return entries, timetable_map


def timetable_columns_for_batch(batch):
    semester = batch.semester if batch else 1
    return get_timetable_columns(semester)


def selected_subject_ids_from_form():
    raw_values = request.form.getlist("subject_ids")
    selected_ids = []
    for value in raw_values:
        try:
            selected_ids.append(int(value))
        except (TypeError, ValueError):
            continue
    return selected_ids


def normalized_department(value):
    return " ".join((value or "").split()).strip()


def subject_total_demand(theory_lectures, has_lab, lab_sessions):
    return theory_lectures + ((lab_sessions if has_lab else 0) * 2)


def subject_record_total_demand(subject):
    return subject_total_demand(
        theory_lectures=subject.theory_lectures_per_week,
        has_lab=subject.has_lab,
        lab_sessions=subject.lab_sessions_per_week,
    )


def active_subjects_query(department, semester):
    return Subject.query.filter_by(
        department=department,
        semester=semester,
        is_active=True,
    )


def available_departments():
    department_rows = (
        db.session.query(Subject.department)
        .distinct()
        .order_by(Subject.department)
        .all()
    )
    return [row[0] for row in department_rows if row[0]]


def available_batches():
    return TimetableBatch.query.order_by(
        TimetableBatch.created_at.desc(), TimetableBatch.id.desc()
    ).all()


def current_subject_context():
    department = normalized_department(request.values.get("department", ""))
    semester = request.values.get("semester", type=int)
    if semester not in SEMESTER_OPTIONS:
        semester = None
    return department, semester


def selected_edit_subject():
    subject_id = request.values.get("edit_subject_id", type=int)
    if not subject_id:
        return None
    return Subject.query.get(subject_id)


def current_department():
    return normalized_department(request.values.get("department", ""))


def selected_edit_faculty():
    faculty_id = request.values.get("edit_faculty_id", type=int)
    if not faculty_id:
        return None
    return Faculty.query.get(faculty_id)


def department_subjects(department):
    if not department:
        return []
    return (
        Subject.query.filter_by(department=department, is_active=True)
        .order_by(Subject.semester.asc(), Subject.code.asc())
        .all()
    )


def faculty_capability_subject_ids():
    subject_ids = set()
    for raw_value in request.form.getlist("subject_ids"):
        try:
            subject_id = int(raw_value)
        except (TypeError, ValueError):
            continue
        subject_ids.add(subject_id)
    return subject_ids


def active_faculty_capabilities_query(department):
    return (
        FacultyCapability.query.join(Faculty).join(Subject).filter(
            Faculty.department == department,
            Faculty.is_active.is_(True),
            Subject.is_active.is_(True),
        )
    )


def faculty_generation_candidates(subjects, section="A"):
    candidates = []
    for subject in subjects:
        subject_capabilities = (
            active_faculty_capabilities_query(subject.department)
            .filter(FacultyCapability.subject_id == subject.id)
            .order_by(Faculty.name.asc())
            .all()
        )
        for capability in subject_capabilities:
            candidates.append(
                SimpleNamespace(
                    faculty_id=capability.faculty_id,
                    faculty=capability.faculty,
                    subject_id=subject.id,
                    subject=subject,
                    preferred_section=section,
                )
            )
    return candidates


def uncovered_subject_codes(subjects):
    uncovered = []
    for subject in subjects:
        has_active_faculty = (
            active_faculty_capabilities_query(subject.department)
            .filter(FacultyCapability.subject_id == subject.id)
            .first()
        )
        if not has_active_faculty:
            uncovered.append(subject.code)
    return uncovered


def validate_subject_capacity(department, semester, demand, subject_id=None):
    active_subjects = Subject.query.filter_by(
        department=department,
        semester=semester,
        is_active=True,
    )
    if subject_id:
        active_subjects = active_subjects.filter(Subject.id != subject_id)
    current_demand = sum(subject_record_total_demand(subject) for subject in active_subjects.all())
    return current_demand + demand <= MAX_WEEKLY_TEACHING_SLOTS


@main.route("/")
def index():
    user = current_user()
    if not user:
        return redirect(url_for("main.login"))
    if user.role == "admin":
        return redirect(url_for("main.admin_dashboard"))
    return redirect(url_for("main.faculty_dashboard"))


@main.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not name or not email or not password:
            flash("Name, email, and password are required.", "error")
            return redirect(url_for("main.signup"))

        existing_user = User.query.filter_by(email=email).first()
        existing_faculty = Faculty.query.filter_by(email=email).first()
        if existing_user or existing_faculty:
            flash("Signup failed. Email may already exist.", "error")
            return redirect(url_for("main.signup"))

        user = User(name=name, email=email, password_hash=generate_password_hash(password), role="faculty")
        faculty = Faculty(name=name, email=email, user=user)
        db.session.add_all([user, faculty])

        try:
            db.session.commit()
            flash("Faculty account created. Please log in.", "success")
            return redirect(url_for("main.login"))
        except IntegrityError:
            db.session.rollback()
            flash("Signup failed due to existing data in the database. Try a different email.", "error")
            return redirect(url_for("main.signup"))

    return render_template("signup.html")


@main.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter_by(email=email).first()

        if not user or not check_password_hash(user.password_hash, password):
            flash("Invalid email or password.", "error")
            return redirect(url_for("main.login"))

        session["user_id"] = user.id
        session["role"] = user.role

        if user.role == "admin":
            return redirect(url_for("main.admin_dashboard"))
        return redirect(url_for("main.faculty_dashboard"))

    return render_template("login.html")


@main.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("main.login"))


@main.route("/admin", methods=["GET"])
def admin_dashboard():
    user = current_user()
    if not user or user.role != "admin":
        flash("Admin login required.", "error")
        return redirect(url_for("main.login"))

    selected_department, selected_semester = current_subject_context()
    filtered_subjects = []
    if selected_department and selected_semester:
        filtered_subjects = (
            active_subjects_query(selected_department, selected_semester)
            .order_by(Subject.name)
            .all()
        )

    batches = available_batches()
    selected_batch_id = request.args.get("batch_id", type=int)
    selected_batch = None
    if selected_batch_id:
        selected_batch = TimetableBatch.query.get(selected_batch_id)
    if not selected_batch:
        selected_batch = batches[0] if batches else None
    faculties = Faculty.query.order_by(Faculty.name).all()
    capabilities = (
        FacultyCapability.query.join(Faculty).join(Subject)
        .filter(Faculty.is_active.is_(True), Subject.is_active.is_(True))
        .order_by(Faculty.department.asc(), Faculty.name.asc(), Subject.code.asc())
        .all()
    )
    entries = []
    if selected_batch:
        entries = (
            TimetableEntry.query.filter_by(batch_id=selected_batch.id)
            .order_by(TimetableEntry.section, TimetableEntry.day, TimetableEntry.time_slot)
            .all()
        )

    return render_template(
        "admin_dashboard.html",
        subjects=filtered_subjects,
        semester_options=SEMESTER_OPTIONS,
        batches=batches,
        selected_batch=selected_batch,
        faculties=faculties,
        capabilities=capabilities,
        timetable_entries=entries,
        faculty_count=len(faculties),
        subject_count=Subject.query.count(),
        registration_count=len(capabilities),
        timetable_count=len(entries),
        available_departments=available_departments(),
        selected_department=selected_department,
        selected_semester=selected_semester,
        max_weekly_teaching_slots=MAX_WEEKLY_TEACHING_SLOTS,
    )


@main.route("/admin/subjects", methods=["GET", "POST"])
def admin_subjects():
    user = current_user()
    if not user or user.role != "admin":
        flash("Admin login required.", "error")
        return redirect(url_for("main.login"))

    selected_department, selected_semester = current_subject_context()
    edit_subject = selected_edit_subject()

    if request.method == "POST":
        edit_subject_id = request.form.get("edit_subject_id", type=int)
        edit_subject = Subject.query.get(edit_subject_id) if edit_subject_id else None
        department = normalized_department(request.form.get("department", ""))
        semester = request.form.get("semester", type=int)
        code = request.form.get("code", "").strip().upper()
        name = request.form.get("name", "").strip()
        theory_lectures = request.form.get("theory_lectures_per_week", type=int)
        has_lab = request.form.get("has_lab") == "on"
        lab_sessions = request.form.get("lab_sessions_per_week", type=int)

        if not department or semester not in SEMESTER_OPTIONS:
            flash("Select a department and semester before configuring subjects.", "error")
            return redirect(url_for("main.admin_subjects"))

        theory_lectures = max(theory_lectures or 0, 0)
        lab_sessions = max(lab_sessions or 0, 0)
        total_demand = subject_total_demand(theory_lectures, has_lab, lab_sessions)

        if not code or not name:
            flash("Subject code and subject name are required.", "error")
            return redirect(
                url_for(
                    "main.admin_subjects",
                    department=department,
                    semester=semester,
                    edit_subject_id=edit_subject_id,
                )
            )
        existing_subject = Subject.query.filter_by(code=code).first()
        if existing_subject and (not edit_subject or existing_subject.id != edit_subject.id):
            flash("Subject code already exists. Use a different code.", "error")
            return redirect(
                url_for(
                    "main.admin_subjects",
                    department=department,
                    semester=semester,
                    edit_subject_id=edit_subject_id,
                )
            )
        if total_demand <= 0:
            flash("A subject must have at least one weekly theory lecture or lab session.", "error")
            return redirect(
                url_for(
                    "main.admin_subjects",
                    department=department,
                    semester=semester,
                    edit_subject_id=edit_subject_id,
                )
            )
        if total_demand > MAX_WEEKLY_TEACHING_SLOTS:
            flash("This subject alone exceeds the weekly teaching capacity.", "error")
            return redirect(
                url_for(
                    "main.admin_subjects",
                    department=department,
                    semester=semester,
                    edit_subject_id=edit_subject_id,
                )
            )
        if not validate_subject_capacity(
            department,
            semester,
            total_demand,
            subject_id=edit_subject.id if edit_subject else None,
        ):
            flash(
                "Saving this subject would exceed the weekly teaching capacity for the selected department and semester.",
                "warning",
            )
            return redirect(
                url_for(
                    "main.admin_subjects",
                    department=department,
                    semester=semester,
                    edit_subject_id=edit_subject_id,
                )
            )

        subject = edit_subject or Subject()
        subject.code = code
        subject.name = name
        subject.department = department
        subject.semester = semester
        subject.weekly_slots = theory_lectures
        subject.theory_lectures_per_week = theory_lectures
        subject.has_lab = has_lab
        subject.lab_sessions_per_week = lab_sessions if has_lab else 0
        subject.is_lab = has_lab and theory_lectures == 0
        subject.is_subject_linked_lab = has_lab
        subject.is_priority = False
        subject.is_active = True
        db.session.add(subject)
        try:
            db.session.commit()
            flash(
                "Subject configuration updated." if edit_subject else "Subject configuration saved.",
                "success",
            )
        except IntegrityError:
            db.session.rollback()
            flash("Subject configuration could not be saved.", "error")

        return redirect(url_for("main.admin_subjects", department=department, semester=semester))

    subjects = []
    if selected_department and selected_semester:
        subjects = (
            Subject.query.filter_by(
                department=selected_department,
                semester=selected_semester,
                is_active=True,
            )
            .order_by(Subject.code.asc(), Subject.id.asc())
            .all()
        )

    return render_template(
        "subject_config.html",
        subjects=subjects,
        semester_options=SEMESTER_OPTIONS,
        available_departments=available_departments(),
        selected_department=selected_department,
        selected_semester=selected_semester,
        edit_subject=edit_subject,
        max_weekly_teaching_slots=MAX_WEEKLY_TEACHING_SLOTS,
        configured_demand=sum(
            subject_record_total_demand(subject) for subject in subjects if subject.is_active
        ),
    )


@main.route("/admin/subjects/<int:subject_id>/delete", methods=["POST"])
def delete_subject(subject_id):
    user = current_user()
    if not user or user.role != "admin":
        flash("Admin login required.", "error")
        return redirect(url_for("main.login"))

    subject = db.get_or_404(Subject, subject_id)
    department = normalized_department(request.form.get("department", subject.department))
    semester = request.form.get("semester", type=int) or subject.semester
    db.session.delete(subject)
    db.session.commit()
    flash("Subject removed.", "success")
    return redirect(url_for("main.admin_subjects", department=department, semester=semester))


@main.route("/admin/faculty", methods=["GET", "POST"])
def admin_faculty():
    user = current_user()
    if not user or user.role != "admin":
        flash("Admin login required.", "error")
        return redirect(url_for("main.login"))

    selected_department = current_department()
    edit_faculty = selected_edit_faculty()

    if request.method == "POST":
        edit_faculty_id = request.form.get("edit_faculty_id", type=int)
        edit_faculty = Faculty.query.get(edit_faculty_id) if edit_faculty_id else None
        department = normalized_department(request.form.get("department", ""))
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower() or None
        max_weekly_load = max(request.form.get("max_weekly_load", type=int) or 0, 0)
        is_active = request.form.get("is_active") == "on"
        selected_subject_ids = faculty_capability_subject_ids()

        if not department:
            flash("Select a department before configuring faculty.", "error")
            return redirect(url_for("main.admin_faculty"))
        if not name:
            flash("Faculty name is required.", "error")
            return redirect(
                url_for(
                    "main.admin_faculty",
                    department=department,
                    edit_faculty_id=edit_faculty_id,
                )
            )
        if not selected_subject_ids:
            flash("Select at least one subject capability before saving a faculty record.", "error")
            return redirect(
                url_for(
                    "main.admin_faculty",
                    department=department,
                    edit_faculty_id=edit_faculty_id,
                )
            )

        valid_subject_ids = {subject.id for subject in department_subjects(department)}
        if not selected_subject_ids.issubset(valid_subject_ids):
            flash("Faculty capability selection must only include active subjects from the selected department.", "error")
            return redirect(
                url_for(
                    "main.admin_faculty",
                    department=department,
                    edit_faculty_id=edit_faculty_id,
                )
            )

        existing_email_faculty = Faculty.query.filter_by(email=email).first() if email else None
        if existing_email_faculty and (not edit_faculty or existing_email_faculty.id != edit_faculty.id):
            flash("That faculty email is already in use.", "error")
            return redirect(
                url_for(
                    "main.admin_faculty",
                    department=department,
                    edit_faculty_id=edit_faculty_id,
                )
            )

        faculty = edit_faculty or Faculty()
        faculty.name = name
        faculty.email = email
        faculty.department = department
        faculty.max_weekly_load = max_weekly_load
        faculty.is_active = is_active
        db.session.add(faculty)
        db.session.flush()

        FacultyCapability.query.filter_by(faculty_id=faculty.id).delete()
        for subject_id in sorted(selected_subject_ids):
            db.session.add(FacultyCapability(faculty_id=faculty.id, subject_id=subject_id))

        try:
            db.session.commit()
            if max_weekly_load > MAX_WEEKLY_TEACHING_SLOTS:
                flash(
                    "Faculty saved. The maximum weekly load is above the normal timetable capacity and may need review.",
                    "warning",
                )
            else:
                flash(
                    "Faculty configuration updated." if edit_faculty else "Faculty configuration saved.",
                    "success",
                )
        except IntegrityError:
            db.session.rollback()
            flash("Faculty configuration could not be saved.", "error")

        return redirect(url_for("main.admin_faculty", department=department))

    faculty_members = []
    if selected_department:
        faculty_members = (
            Faculty.query.filter_by(department=selected_department)
            .order_by(Faculty.name.asc(), Faculty.id.asc())
            .all()
        )

    return render_template(
        "faculty_config.html",
        available_departments=available_departments(),
        selected_department=selected_department,
        subjects=department_subjects(selected_department),
        faculty_members=faculty_members,
        edit_faculty=edit_faculty,
        max_weekly_teaching_slots=MAX_WEEKLY_TEACHING_SLOTS,
    )


@main.route("/faculty", methods=["GET"])
def faculty_dashboard():
    user = current_user()
    if not user or user.role != "faculty":
        flash("Faculty login required.", "error")
        return redirect(url_for("main.login"))

    faculty = Faculty.query.filter_by(user_id=user.id).first()
    if not faculty:
        flash("Faculty profile was not found.", "error")
        return redirect(url_for("main.logout"))

    capabilities = (
        FacultyCapability.query.filter_by(faculty_id=faculty.id)
        .join(Subject)
        .filter(Subject.is_active.is_(True))
        .order_by(Subject.department.asc(), Subject.semester.asc(), Subject.code.asc())
        .all()
    )
    batches = available_batches()
    selected_batch_id = request.args.get("batch_id", type=int)
    selected_batch = None
    if selected_batch_id:
        selected_batch = TimetableBatch.query.get(selected_batch_id)
    if not selected_batch:
        selected_batch = batches[0] if batches else None
    entries, timetable_map = faculty_timetable_rows(
        faculty.id,
        selected_batch.id if selected_batch else None,
    )

    return render_template(
        "faculty_dashboard.html",
        faculty=faculty,
        capabilities=capabilities,
        batches=batches,
        selected_batch=selected_batch,
        entries=entries,
        timetable_map=timetable_map,
        days=DAYS,
        timetable_columns=timetable_columns_for_batch(selected_batch),
    )


@main.route("/generate", methods=["POST"])
def generate():
    user = current_user()
    if not user or user.role != "admin":
        flash("Admin login required.", "error")
        return redirect(url_for("main.login"))

    department = normalized_department(request.form.get("department", ""))
    semester = request.form.get("semester", type=int)

    if not department or semester not in SEMESTER_OPTIONS:
        flash("Select a department and semester before generating the timetable.", "error")
        return redirect(url_for("main.admin_dashboard"))

    active_subjects = (
        active_subjects_query(department, semester)
        .order_by(Subject.name)
        .all()
    )
    if not active_subjects:
        flash(
            "No active subjects are configured for the selected department and semester. Configure subjects first.",
            "error",
        )
        return redirect(url_for("main.admin_subjects", department=department, semester=semester))

    missing_faculty_subjects = uncovered_subject_codes(active_subjects)
    if missing_faculty_subjects:
        flash(
            "Timetable generation is blocked. Add active faculty capability mapping for: "
            + ", ".join(missing_faculty_subjects),
            "error",
        )
        return redirect(url_for("main.admin_faculty", department=department))

    selected_subject_ids = selected_subject_ids_from_form()
    if not selected_subject_ids:
        flash("Select at least one active subject before generating the timetable.", "error")
        return redirect(
            url_for("main.admin_dashboard", department=department, semester=semester)
        )

    selected_subjects = [subject for subject in active_subjects if subject.id in selected_subject_ids]
    missing_selected_subjects = uncovered_subject_codes(selected_subjects)
    if missing_selected_subjects:
        flash(
            "Timetable generation is blocked. Complete faculty capability mapping for: "
            + ", ".join(missing_selected_subjects),
            "error",
        )
        return redirect(url_for("main.admin_faculty", department=department))

    registrations = faculty_generation_candidates(selected_subjects)
    if not registrations:
        flash("No active faculty capability mappings exist for the selected subjects.", "error")
        return redirect(
            url_for("main.admin_dashboard", department=department, semester=semester)
        )

    batch_name = (
        request.form.get("batch_name", "").strip()
        or f"{department} Semester {semester} Timetable"
    )
    batch, _, unassigned = create_timetable_batch(registrations, semester)
    batch.name = batch_name
    db.session.commit()

    if unassigned:
        flash(
            f'Timetable "{batch.name}" created with {len(unassigned)} unassigned demand item(s).',
            "warning",
        )
    else:
        flash(f'Timetable "{batch.name}" created successfully.', "success")

    return redirect(url_for("main.admin_dashboard", batch_id=batch.id))


@main.route("/admin/timetable/<int:batch_id>/delete", methods=["POST"])
def delete_timetable(batch_id):
    user = current_user()
    if not user or user.role != "admin":
        flash("Admin login required.", "error")
        return redirect(url_for("main.login"))

    batch = db.get_or_404(TimetableBatch, batch_id)
    batch_name = batch.name
    db.session.delete(batch)
    db.session.commit()
    flash(f'Timetable "{batch_name}" deleted.', "success")
    return redirect(url_for("main.admin_dashboard"))


@main.route("/admin/timetable")
def admin_timetable():
    user = current_user()
    if not user or user.role != "admin":
        flash("Admin login required.", "error")
        return redirect(url_for("main.login"))

    batches = available_batches()
    selected_batch_id = request.args.get("batch_id", type=int)
    selected_batch = None
    if selected_batch_id:
        selected_batch = TimetableBatch.query.get(selected_batch_id)
    if not selected_batch:
        selected_batch = batches[0] if batches else None

    entries = []
    if selected_batch:
        entries = (
            TimetableEntry.query.filter_by(batch_id=selected_batch.id)
            .order_by(TimetableEntry.section, TimetableEntry.day, TimetableEntry.time_slot)
            .all()
        )
    sections = sorted({entry.section for entry in entries})
    timetable_map = defaultdict(dict)
    for entry in entries:
        timetable_map[(entry.section, entry.day)][entry.time_slot] = entry

    return render_template(
        "admin_timetable.html",
        batches=batches,
        selected_batch=selected_batch,
        entries=entries,
        sections=sections,
        timetable_map=timetable_map,
        days=DAYS,
        timetable_columns=timetable_columns_for_batch(selected_batch),
    )
