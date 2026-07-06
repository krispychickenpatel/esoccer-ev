import os

try:  # every entry point imports this module -- load .env exactly once, here,
    # not per-script (main.py's own load_dotenv call was the bug: it never
    # fired for `python -m app.backfill` since backfill.py doesn't import main)
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # fall back to real environment variables

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from .models import Base, Settings

# SQLite for local prototype. Set DATABASE_URL=postgresql+psycopg://... to migrate.
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./esoccer.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

# The old v0.3.4 shipped default -- rows still holding this exact value have
# never been customized via Settings, so it's safe to move them to the new
# bet365-only default. Rows a user already edited are left untouched.
_OLD_SPORTSBOOKS_DEFAULT = '["fanduel", "bet365"]'
_NEW_SPORTSBOOKS_DEFAULT = '["bet365"]'


def _migrate_add_missing_columns() -> None:
    """Additive-only schema evolution: add model columns that don't exist yet
    on an already-created table, and backfill their scalar default for
    existing rows. Never drops or rewrites existing columns/data -- v0.3.4
    data (matches, odds, bets, prediction ledger, etc.) must survive this
    unchanged.

    Backfill is a SEPARATE pass over every scalar-default column (not just
    ones added in this run) so it's self-healing: SQLite auto-commits DDL
    (ALTER TABLE) independently of the surrounding Python transaction, so a
    column can end up added-but-not-backfilled if an unrelated error
    interrupts a previous run between the ADD and the UPDATE. Re-running
    this function always repairs that -- it never re-touches a row that
    already holds a real (non-NULL) value.

    All schema *inspection* happens first, on its own connection, fully
    closed before any write transaction opens. Interleaving insp.* calls
    with an open `engine.begin()` write transaction (as an earlier version
    of this function did) causes SQLite's rollback-journal mode to lock the
    inspector's connection against the writer's pending transaction --
    self-inflicted "database is locked" errors from a single process."""
    insp = inspect(engine)
    plan: list[tuple[str, str, str]] = []  # (table_name, column_name, compiled_type)
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        existing_cols = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name not in existing_cols:
                plan.append((table.name, col.name, col.type.compile(dialect=engine.dialect)))

    with engine.begin() as conn:
        for table_name, col_name, col_type in plan:
            conn.execute(text(f'ALTER TABLE "{table_name}" ADD COLUMN "{col_name}" {col_type}'))
        for table in Base.metadata.sorted_tables:
            for col in table.columns:
                if col.default is not None and getattr(col.default, "is_scalar", False):
                    conn.execute(
                        text(f'UPDATE "{table.name}" SET "{col.name}" = :default WHERE "{col.name}" IS NULL')
                        .bindparams(default=col.default.arg))


def init_db() -> None:
    Base.metadata.create_all(engine)
    _migrate_add_missing_columns()
    with SessionLocal() as db:
        if db.get(Settings, 1) is None:
            db.add(Settings(id=1))
            db.commit()
        else:
            s = db.get(Settings, 1)
            if s.sportsbooks_tracked == _OLD_SPORTSBOOKS_DEFAULT:
                s.sportsbooks_tracked = _NEW_SPORTSBOOKS_DEFAULT
                db.commit()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
