from flask import Flask
from sqlalchemy import inspect, text
from werkzeug.security import generate_password_hash

from .models import TimetableBatch, User, db


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "dev-secret-key"
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///timetable.db"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)

    from .routes import main

    app.register_blueprint(main)

    with app.app_context():
        db.create_all()
        ensure_schema()
        seed_admin()

    return app


def ensure_schema():
    inspector = inspect(db.engine)
    table_names = set(inspector.get_table_names())

    if "faculty" in table_names:
        faculty_uniques = inspector.get_unique_constraints("faculty")
        faculty_unique_columns = {tuple(sorted(item.get("column_names") or [])) for item in faculty_uniques}
        faculty_columns = {column["name"] for column in inspector.get_columns("faculty")}
        if ("name",) in faculty_unique_columns or "user_id" not in faculty_columns:
            rebuild_faculty_table(has_user_id="user_id" in faculty_columns)

    if "faculty_subject_registration" in table_names:
        registration_columns = {
            column["name"] for column in inspector.get_columns("faculty_subject_registration")
        }
        if "status" not in registration_columns:
            db.session.execute(
                text(
                    "ALTER TABLE faculty_subject_registration "
                    "ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'approved'"
                )
            )
            db.session.commit()

    if "subject" in table_names:
        subject_columns = {column["name"] for column in inspector.get_columns("subject")}
        if "semester" not in subject_columns:
            db.session.execute(
                text("ALTER TABLE subject ADD COLUMN semester INTEGER NOT NULL DEFAULT 1")
            )
            db.session.commit()

    if "timetable_batch" not in table_names:
        TimetableBatch.__table__.create(bind=db.engine)

    if "timetable_entry" in table_names:
        entry_columns = {column["name"] for column in inspector.get_columns("timetable_entry")}
        if "batch_id" not in entry_columns:
            rebuild_timetable_entry_table()
        attach_legacy_entries_to_batch()


def rebuild_faculty_table(has_user_id):
    select_user_id = "user_id" if has_user_id else "NULL AS user_id"

    db.session.execute(text("PRAGMA foreign_keys=OFF"))
    db.session.execute(text("ALTER TABLE faculty RENAME TO faculty_old"))
    db.session.execute(
        text(
            """
            CREATE TABLE faculty (
                id INTEGER NOT NULL PRIMARY KEY,
                name VARCHAR(120) NOT NULL,
                email VARCHAR(120) UNIQUE,
                user_id INTEGER UNIQUE,
                FOREIGN KEY(user_id) REFERENCES user (id)
            )
            """
        )
    )
    db.session.execute(
        text(
            f"""
            INSERT INTO faculty (id, name, email, user_id)
            SELECT id, name, email, {select_user_id}
            FROM faculty_old
            """
        )
    )
    db.session.execute(text("DROP TABLE faculty_old"))
    db.session.execute(text("PRAGMA foreign_keys=ON"))
    db.session.commit()


def rebuild_timetable_entry_table():
    db.session.execute(text("PRAGMA foreign_keys=OFF"))
    db.session.execute(text("ALTER TABLE timetable_entry RENAME TO timetable_entry_old"))
    db.session.execute(
        text(
            """
            CREATE TABLE timetable_entry (
                id INTEGER NOT NULL PRIMARY KEY,
                batch_id INTEGER,
                day VARCHAR(20) NOT NULL,
                time_slot VARCHAR(30) NOT NULL,
                section VARCHAR(30) NOT NULL,
                faculty_id INTEGER NOT NULL,
                subject_id INTEGER NOT NULL,
                FOREIGN KEY(batch_id) REFERENCES timetable_batch (id),
                FOREIGN KEY(faculty_id) REFERENCES faculty (id),
                FOREIGN KEY(subject_id) REFERENCES subject (id),
                CONSTRAINT uq_batch_section_day_time UNIQUE (batch_id, day, time_slot, section)
            )
            """
        )
    )
    db.session.execute(
        text(
            """
            INSERT INTO timetable_entry (id, day, time_slot, section, faculty_id, subject_id)
            SELECT id, day, time_slot, section, faculty_id, subject_id
            FROM timetable_entry_old
            """
        )
    )
    db.session.execute(text("DROP TABLE timetable_entry_old"))
    db.session.execute(text("PRAGMA foreign_keys=ON"))
    db.session.commit()


def attach_legacy_entries_to_batch():
    has_legacy_rows = db.session.execute(
        text("SELECT 1 FROM timetable_entry WHERE batch_id IS NULL LIMIT 1")
    ).scalar()
    if not has_legacy_rows:
        return

    legacy_batch = TimetableBatch.query.filter_by(name="Legacy Timetable").first()
    if not legacy_batch:
        legacy_batch = TimetableBatch(name="Legacy Timetable", semester=0)
        db.session.add(legacy_batch)
        db.session.commit()

    db.session.execute(
        text("UPDATE timetable_entry SET batch_id = :batch_id WHERE batch_id IS NULL"),
        {"batch_id": legacy_batch.id},
    )
    db.session.commit()


def seed_admin():
    admin_email = "tanishs1213@gmail.com"
    admin = User.query.filter_by(email=admin_email).first()
    password_hash = generate_password_hash("admin")
    if admin:
        admin.name = "Admin"
        admin.password_hash = password_hash
        admin.role = "admin"
        db.session.commit()
        return

    db.session.add(User(name="Admin", email=admin_email, password_hash=password_hash, role="admin"))
    db.session.commit()
