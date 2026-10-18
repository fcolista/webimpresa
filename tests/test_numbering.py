from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from app.db import transaction
from app.operations import _next_number


def test_number_allocation_is_atomic_under_concurrency(app):
    def allocate(_):
        with app.app_context():
            with transaction() as db:
                return _next_number(db, "quote", 2040)

    with ThreadPoolExecutor(max_workers=8) as pool:
        numbers = list(pool.map(allocate, range(24)))
    assert len(set(numbers)) == 24
    assert set(numbers) == {f"2040-{number:03d}" for number in range(1, 25)}


def test_number_allocator_skips_manual_numbers_and_continues_after_large_sequence(app):
    with app.app_context():
        from app.db import get_db
        db = get_db()
        db.execute(
            """INSERT INTO clients(kind,name,surname) VALUES('person','Test','Cliente')"""
        )
        client_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        user_id = db.execute("SELECT id FROM users WHERE username='operatore'").fetchone()[0]
        db.execute(
            """INSERT INTO receipts(number,year,client_id,client_snapshot,provider_snapshot,status,issue_date,
               subject,tax_config,totals,created_by)
               VALUES('2041-1000',2041,?,'{}','{}','draft','2041-01-01','Test','{}','{}',?)""",
            (client_id, user_id),
        )
        with transaction() as connection:
            assert _next_number(connection, "receipt", 2041) == "2041-1001"
