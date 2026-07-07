"""v0.3.7B: watchdog health endpoint, max-entry semantics labeling,
friend-pick retro-CSV exclusion, spot-check field separation."""
import csv
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.engines import entry_floor_diagnostics
from app.models import Base, Settings
from app.routers.ops import health

FRIEND_CSV = Path("/Users/krispatell/Downloads/ESoccer/notes/friend_picks.csv")
SPOT_CHECK_SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "spot_check_capture.py"


def _db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = Session()
    db.add(Settings(id=1))
    db.commit()
    return db


def test_health_endpoint_reports_status_on_empty_db():
    db = _db()
    result = health(db=db)
    assert "collector_alive" in result
    assert "db_writable" in result
    assert result["db_writable"] is True
    assert "status" in result
    assert result["snapshots_created_today"] == 0


def test_max_entry_semantics_labeled_correctly_in_report():
    db = _db()
    result = entry_floor_diagnostics.run(db)
    assert "analysis_only_disclaimer" in result
    assert "floor_equals_signal_price_count" in result
    # must never claim entry-logic was changed
    assert "does not change entry" in result["analysis_only_disclaimer"]


def test_friend_pick_retro_row_excluded_from_clean_sample():
    assert FRIEND_CSV.exists(), "notes/friend_picks.csv must exist"
    with open(FRIEND_CSV) as f:
        rows = list(csv.DictReader(f))
    assert len(rows) >= 1
    luis_row = next(r for r in rows if r["source"] == "Luis")
    assert luis_row["clean_scored"] == "FALSE"
    assert luis_row["logged_after_result"] == "TRUE"
    assert luis_row["was_pick_cancelled_by_source"] == "FALSE"
    assert luis_row["was_market_unavailable"] == "TRUE"
    assert "retro_result_known" in luis_row["exclude_reason"]


def test_spot_check_capture_separates_provider_and_book_fields():
    assert SPOT_CHECK_SCRIPT.exists()
    text = SPOT_CHECK_SCRIPT.read_text()
    for provider_field in ("provider_latest_price", "provider_source_ts",
                           "provider_polled_at", "provider_ingested_at"):
        assert provider_field in text
    for book_field in ("displayed_price", "market_available_on_book"):
        assert book_field in text
    # provider fields must never be named the same as the book fields
    assert "provider_latest_price" != "displayed_price"
