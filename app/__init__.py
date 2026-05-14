from flask import Flask
from sqlalchemy import inspect, text
from werkzeug.security import generate_password_hash
import os

from .models import FacultyCapability, TimetableBatch, User, db

def create_app():
    app = Flask(__name__)
    secret_key = os.environ.get("SECRET_KEY")
    if not secret_key:
        import warnings
        warnings.warn(
            "SECRET_KEY environment variable is not set. Using insecure default. "
            "Sessions WILL break across serverless cold starts on Vercel. "
            "Set SECRET_KEY in your Vercel project environment variables.",
            RuntimeWarning,
            stacklevel=2,
        )
        secret_key = "dev-secret-key-change-me"
    app.config["SECRET_KEY"] = secret_key
    
    database_url = os.environ.get("DATABASE_URL")
    if database_url and database_url.strip():
        database_url = database_url.strip()
        # Handle the case where Supabase/Heroku provides 'postgres://' 
        # but SQLAlchemy requires 'postgresql://'
        if database_url.startswith("postgres://"):
            database_url = database_url.replace("postgres://", "postgresql+pg8000://", 1)
        elif database_url.startswith("postgresql://"):
            database_url = database_url.replace("postgresql://", "postgresql+pg8000://", 1)
        app.config["SQLALCHEMY_DATABASE_URI"] = database_url
    else:
        # Vercel's filesystem is read-only; /tmp is the only writable directory.
        # Fall back to a local path when running locally (non-Vercel).
        if os.environ.get("VERCEL") or os.environ.get("VERCEL_ENV"):
            sqlite_path = "/tmp/timetable.db"
        else:
            sqlite_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "instance", "timetable.db")
        app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{sqlite_path}"
        
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    
    # Configure connection pooling to handle PostgreSQL connection drops
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_pre_ping": True,
        "pool_recycle": 300,
    }

    db.init_app(app)

    from .routes import main

    app.register_blueprint(main)

    with app.app_context():
        try:
            db.create_all()
            ensure_schema()
            seed_admin()
        except Exception as e:
            # On Vercel build environment, database access might fail
            # We log the error but allow the app to initialize so Vercel can find the entrypoint
            app.logger.warning(f"Database initialization skipped or failed: {e}")

    return app


def ensure_schema():
    inspector = inspect(db.engine)
    table_names = set(inspector.get_table_names())

    if "user" in table_names:
        user_columns = {column["name"] for column in inspector.get_columns("user")}
        if "department" not in user_columns:
            db.session.execute(
                text("ALTER TABLE user ADD COLUMN department VARCHAR(120) NOT NULL DEFAULT 'General'")
            )
            db.session.commit()

    if "faculty" in table_names:
        faculty_uniques = inspector.get_unique_constraints("faculty")
        faculty_unique_columns = {tuple(sorted(item.get("column_names") or [])) for item in faculty_uniques}
        faculty_columns = {column["name"] for column in inspector.get_columns("faculty")}
        if ("name",) in faculty_unique_columns or "user_id" not in faculty_columns:
            rebuild_faculty_table(has_user_id="user_id" in faculty_columns)
            faculty_columns = {column["name"] for column in inspector.get_columns("faculty")}
        if "department" not in faculty_columns:
            db.session.execute(
                text("ALTER TABLE faculty ADD COLUMN department VARCHAR(120) NOT NULL DEFAULT 'General'")
            )
            db.session.commit()
        if "max_weekly_load" not in faculty_columns:
            db.session.execute(
                text("ALTER TABLE faculty ADD COLUMN max_weekly_load INTEGER NOT NULL DEFAULT 21")
            )
            db.session.commit()
        if "is_active" not in faculty_columns:
            db.session.execute(
                text("ALTER TABLE faculty ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT 1")
            )
            db.session.commit()

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
        if "is_lab" not in subject_columns:
            db.session.execute(
                text("ALTER TABLE subject ADD COLUMN is_lab BOOLEAN NOT NULL DEFAULT 0")
            )
            db.session.commit()
        if "is_subject_linked_lab" not in subject_columns:
            db.session.execute(
                text(
                    "ALTER TABLE subject ADD COLUMN is_subject_linked_lab "
                    "BOOLEAN NOT NULL DEFAULT 0"
                )
            )
            db.session.commit()
        if "is_free_lecture" not in subject_columns:
            db.session.execute(
                text("ALTER TABLE subject ADD COLUMN is_free_lecture BOOLEAN NOT NULL DEFAULT 0")
            )
            db.session.commit()
        if "department" not in subject_columns:
            db.session.execute(
                text("ALTER TABLE subject ADD COLUMN department VARCHAR(120) NOT NULL DEFAULT 'General'")
            )
            db.session.commit()
        if "theory_lectures_per_week" not in subject_columns:
            db.session.execute(
                text(
                    "ALTER TABLE subject ADD COLUMN theory_lectures_per_week "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            )
            db.session.execute(
                text(
                    "UPDATE subject SET theory_lectures_per_week = weekly_slots "
                    "WHERE theory_lectures_per_week = 0 AND COALESCE(is_lab, 0) = 0"
                )
            )
            db.session.commit()
        if "has_lab" not in subject_columns:
            db.session.execute(
                text("ALTER TABLE subject ADD COLUMN has_lab BOOLEAN NOT NULL DEFAULT 0")
            )
            db.session.execute(text("UPDATE subject SET has_lab = COALESCE(is_lab, 0)"))
            db.session.commit()
        if "lab_sessions_per_week" not in subject_columns:
            db.session.execute(
                text(
                    "ALTER TABLE subject ADD COLUMN lab_sessions_per_week "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            )
            db.session.execute(
                text(
                    "UPDATE subject SET lab_sessions_per_week = weekly_slots "
                    "WHERE COALESCE(has_lab, 0) = 1 AND lab_sessions_per_week = 0"
                )
            )
            db.session.commit()
        if "is_priority" in subject_columns and "priority" not in subject_columns:
            # Migrate boolean is_priority -> integer priority (1=high, 3=normal, 5=low)
            db.session.execute(
                text("ALTER TABLE subject ADD COLUMN priority INTEGER NOT NULL DEFAULT 3")
            )
            db.session.execute(
                text("UPDATE subject SET priority = CASE WHEN is_priority = 1 THEN 1 ELSE 3 END")
            )
            db.session.commit()
        elif "priority" not in subject_columns:
            db.session.execute(
                text("ALTER TABLE subject ADD COLUMN priority INTEGER NOT NULL DEFAULT 3")
            )
            db.session.commit()
        if "is_active" not in subject_columns:
            db.session.execute(
                text("ALTER TABLE subject ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT 1")
            )
            db.session.commit()
        if "short_name" not in subject_columns:
            db.session.execute(
                text("ALTER TABLE subject ADD COLUMN short_name VARCHAR(50)")
            )
            db.session.commit()

    if "timetable_batch" not in table_names:
        TimetableBatch.__table__.create(bind=db.engine)

    if "faculty_capability" not in table_names:
        FacultyCapability.__table__.create(bind=db.engine)

    if "timetable_entry" in table_names:
        entry_columns = inspector.get_columns("timetable_entry")
        entry_column_names = {column["name"] for column in entry_columns}
        faculty_id_column = next(
            (column for column in entry_columns if column["name"] == "faculty_id"),
            None,
        )
        if "batch_id" not in entry_column_names or (
            faculty_id_column is not None and not faculty_id_column.get("nullable", False)
        ) or "lab_batch" not in entry_column_names:
            rebuild_timetable_entry_table()
        attach_legacy_entries_to_batch()


def rebuild_faculty_table(has_user_id):
    select_user_id = "user_id" if has_user_id else "NULL AS user_id"

    # PRAGMA statements are SQLite-only
    if db.engine.dialect.name == "sqlite":
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
    if db.engine.dialect.name == "sqlite":
        db.session.execute(text("PRAGMA foreign_keys=ON"))
    db.session.commit()


def rebuild_timetable_entry_table():
    inspector = inspect(db.engine)
    old_columns = {column["name"] for column in inspector.get_columns("timetable_entry")}
    select_batch_id = "batch_id" if "batch_id" in old_columns else "NULL AS batch_id"
    select_lab_batch = "lab_batch" if "lab_batch" in old_columns else "NULL AS lab_batch"
    select_room = "room" if "room" in old_columns else "NULL AS room"

    if db.engine.dialect.name == "sqlite":
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
                lab_batch VARCHAR(30),
                room VARCHAR(50),
                faculty_id INTEGER,
                subject_id INTEGER NOT NULL,
                FOREIGN KEY(batch_id) REFERENCES timetable_batch (id),
                FOREIGN KEY(faculty_id) REFERENCES faculty (id),
                FOREIGN KEY(subject_id) REFERENCES subject (id)
            )
            """
        )
    )
    db.session.execute(
        text(
            f"""
            INSERT INTO timetable_entry (id, batch_id, day, time_slot, section, lab_batch, room, faculty_id, subject_id)
            SELECT id, {select_batch_id}, day, time_slot, section, {select_lab_batch}, {select_room}, faculty_id, subject_id
            FROM timetable_entry_old
            """
        )
    )
    db.session.execute(text("DROP TABLE timetable_entry_old"))
    if db.engine.dialect.name == "sqlite":
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
        admin.department = admin.department or "General"
        db.session.commit()
        return

    db.session.add(
        User(
            name="Admin",
            email=admin_email,
            password_hash=password_hash,
            role="admin",
            department="General",
        )
    )
    db.session.commit()
