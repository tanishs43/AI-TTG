from collections import defaultdict

from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

from .models import (
    Faculty,
    FacultySubjectRegistration,
    Subject,
    TimetableBatch,
    TimetableEntry,
    User,
    db,
)
from .services import DAYS, TIMETABLE_COLUMNS, create_timetable_batch


main = Blueprint("main", __name__)


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    return User.query.get(user_id)


def latest_batch():
    return TimetableBatch.query.order_by(TimetableBatch.created_at.desc(), TimetableBatch.id.desc()).first()


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


def selected_subject_ids_from_form():
    raw_values = request.form.getlist("subject_ids")
    selected_ids = []
    for value in raw_values:
        try:
            selected_ids.append(int(value))
        except (TypeError, ValueError):
            continue
    return selected_ids


def available_semesters():
    semester_rows = (
        db.session.query(Subject.semester).distinct().order_by(Subject.semester).all()
    )
    return [row[0] for row in semester_rows]


def available_batches():
    return TimetableBatch.query.order_by(
        TimetableBatch.created_at.desc(), TimetableBatch.id.desc()
    ).all()


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

    subjects = Subject.query.order_by(Subject.code).all()
    semesters = available_semesters()
    batches = available_batches()
    selected_batch_id = request.args.get("batch_id", type=int)
    selected_batch = None
    if selected_batch_id:
        selected_batch = TimetableBatch.query.get(selected_batch_id)
    if not selected_batch:
        selected_batch = batches[0] if batches else None
    faculties = Faculty.query.order_by(Faculty.name).all()
    registrations = (
        FacultySubjectRegistration.query.order_by(
            FacultySubjectRegistration.preferred_section, FacultySubjectRegistration.id
        ).all()
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
        subjects=subjects,
        semesters=semesters,
        batches=batches,
        selected_batch=selected_batch,
        faculties=faculties,
        registrations=registrations,
        timetable_entries=entries,
        faculty_count=len(faculties),
        subject_count=len(subjects),
        registration_count=len(registrations),
        timetable_count=len(entries),
    )


@main.route("/faculty", methods=["GET", "POST"])
def faculty_dashboard():
    user = current_user()
    if not user or user.role != "faculty":
        flash("Faculty login required.", "error")
        return redirect(url_for("main.login"))

    faculty = Faculty.query.filter_by(user_id=user.id).first()
    if not faculty:
        flash("Faculty profile was not found.", "error")
        return redirect(url_for("main.logout"))

    if request.method == "POST":
        action = request.form.get("action", "").strip()

        if action == "add_subject":
            code = request.form.get("code", "").strip().upper()
            name = request.form.get("name", "").strip()
            semester_raw = request.form.get("semester", "1").strip()
            weekly_slots_raw = request.form.get("weekly_slots", "3").strip()

            try:
                semester = max(int(semester_raw), 1)
            except ValueError:
                semester = 1

            try:
                weekly_slots = max(int(weekly_slots_raw), 1)
            except ValueError:
                weekly_slots = 3

            if not code or not name:
                flash("Subject code and name are required.", "error")
                return redirect(url_for("main.faculty_dashboard"))

            db.session.add(
                Subject(code=code, name=name, semester=semester, weekly_slots=weekly_slots)
            )
            try:
                db.session.commit()
                flash("Subject added.", "success")
            except Exception:
                db.session.rollback()
                flash("Subject could not be added. Code may already exist.", "error")

            return redirect(url_for("main.faculty_dashboard"))

        subject_id = request.form.get("subject_id", type=int)
        preferred_section = request.form.get("preferred_section", "").strip().upper() or "A"

        if not subject_id:
            flash("Select a subject.", "error")
            return redirect(url_for("main.faculty_dashboard"))

        db.session.add(
            FacultySubjectRegistration(
                faculty_id=faculty.id,
                subject_id=subject_id,
                preferred_section=preferred_section,
                status="approved",
            )
        )
        try:
            db.session.commit()
            flash("Subject registration saved for timetable generation.", "success")
        except Exception:
            db.session.rollback()
            flash("This subject is already registered for that section.", "error")

        return redirect(url_for("main.faculty_dashboard"))

    subjects = Subject.query.order_by(Subject.code).all()
    registrations = (
        FacultySubjectRegistration.query.filter_by(faculty_id=faculty.id)
        .order_by(FacultySubjectRegistration.preferred_section)
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
        subjects=subjects,
        registrations=registrations,
        batches=batches,
        selected_batch=selected_batch,
        entries=entries,
        timetable_map=timetable_map,
        days=DAYS,
        timetable_columns=TIMETABLE_COLUMNS,
    )


@main.route("/generate", methods=["POST"])
def generate():
    user = current_user()
    if not user or user.role != "admin":
        flash("Admin login required.", "error")
        return redirect(url_for("main.login"))

    semester_raw = request.form.get("semester", "").strip()
    try:
        semester = int(semester_raw)
    except ValueError:
        semester = None

    if semester is None:
        flash("Select a semester before generating the timetable.", "error")
        return redirect(url_for("main.admin_dashboard"))

    selected_subject_ids = selected_subject_ids_from_form()
    if not selected_subject_ids:
        flash("Select at least one subject before generating the timetable.", "error")
        return redirect(url_for("main.admin_dashboard"))

    registrations = (
        FacultySubjectRegistration.query.join(Subject).filter(
            FacultySubjectRegistration.status == "approved",
            Subject.semester == semester,
            FacultySubjectRegistration.subject_id.in_(selected_subject_ids),
        ).all()
    )
    if not registrations:
        flash("No faculty registrations exist for the selected subjects.", "error")
        return redirect(url_for("main.admin_dashboard"))

    batch_name = request.form.get("batch_name", "").strip() or f"Semester {semester} Timetable"
    batch, _, unassigned = create_timetable_batch(registrations, semester)
    batch.name = batch_name
    db.session.commit()

    if unassigned:
        flash(
            f'Timetable "{batch.name}" created with {len(unassigned)} unassigned registration(s).',
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
        timetable_columns=TIMETABLE_COLUMNS,
    )
