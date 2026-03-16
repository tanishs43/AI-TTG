from flask_sqlalchemy import SQLAlchemy


db = SQLAlchemy()


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), nullable=False, unique=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False)
    faculty_profile = db.relationship("Faculty", back_populates="user", uselist=False)


class Faculty(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False,)
    email = db.Column(db.String(120), nullable=True, unique=True)
    department = db.Column(db.String(120), nullable=False, default="General")
    max_weekly_load = db.Column(db.Integer, nullable=False, default=21)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, unique=True)
    user = db.relationship("User", back_populates="faculty_profile")
    capabilities = db.relationship(
        "FacultyCapability",
        back_populates="faculty",
        cascade="all, delete-orphan",
    )
    registrations = db.relationship(
        "FacultySubjectRegistration",
        back_populates="faculty",
        cascade="all, delete-orphan",
    )


class Subject(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(30), nullable=False, unique=True)
    name = db.Column(db.String(120), nullable=False)
    department = db.Column(db.String(120), nullable=False, default="General")
    semester = db.Column(db.Integer, nullable=False, default=1)
    weekly_slots = db.Column(db.Integer, nullable=False, default=3)
    is_lab = db.Column(db.Boolean, nullable=False, default=False)
    is_subject_linked_lab = db.Column(db.Boolean, nullable=False, default=False)
    theory_lectures_per_week = db.Column(db.Integer, nullable=False, default=3)
    has_lab = db.Column(db.Boolean, nullable=False, default=False)
    lab_sessions_per_week = db.Column(db.Integer, nullable=False, default=0)
    is_priority = db.Column(db.Boolean, nullable=False, default=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    registrations = db.relationship(
        "FacultySubjectRegistration",
        back_populates="subject",
        cascade="all, delete-orphan",
    )
    capabilities = db.relationship(
        "FacultyCapability",
        back_populates="subject",
        cascade="all, delete-orphan",
    )


class FacultyCapability(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    faculty_id = db.Column(db.Integer, db.ForeignKey("faculty.id"), nullable=False)
    subject_id = db.Column(db.Integer, db.ForeignKey("subject.id"), nullable=False)

    faculty = db.relationship("Faculty", back_populates="capabilities")
    subject = db.relationship("Subject", back_populates="capabilities")

    __table_args__ = (
        db.UniqueConstraint("faculty_id", "subject_id", name="uq_faculty_capability"),
    )


class FacultySubjectRegistration(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    faculty_id = db.Column(db.Integer, db.ForeignKey("faculty.id"), nullable=False)
    subject_id = db.Column(db.Integer, db.ForeignKey("subject.id"), nullable=False)
    preferred_section = db.Column(db.String(30), nullable=False, default="A")
    status = db.Column(db.String(20), nullable=False, default="approved")

    faculty = db.relationship("Faculty", back_populates="registrations")
    subject = db.relationship("Subject", back_populates="registrations")

    __table_args__ = (
        db.UniqueConstraint(
            "faculty_id",
            "subject_id",
            "preferred_section",
            name="uq_faculty_subject_section",
        ),
    )


class TimetableBatch(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    semester = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, server_default=db.func.now())
    entries = db.relationship(
        "TimetableEntry",
        back_populates="batch",
        cascade="all, delete-orphan",
    )


class TimetableEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    batch_id = db.Column(db.Integer, db.ForeignKey("timetable_batch.id"), nullable=True)
    day = db.Column(db.String(20), nullable=False)
    time_slot = db.Column(db.String(30), nullable=False)
    section = db.Column(db.String(30), nullable=False)
    faculty_id = db.Column(db.Integer, db.ForeignKey("faculty.id"), nullable=False)
    subject_id = db.Column(db.Integer, db.ForeignKey("subject.id"), nullable=False)

    batch = db.relationship("TimetableBatch", back_populates="entries")
    faculty = db.relationship("Faculty")
    subject = db.relationship("Subject")

    __table_args__ = (
        db.UniqueConstraint(
            "batch_id", "day", "time_slot", "section", name="uq_batch_section_day_time"
        ),
    )
