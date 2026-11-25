from __future__ import annotations

import gzip
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from trend_intelligence.db import MIGRATIONS_DIR, backup, connect, migrate
from trend_intelligence.store import Observation, RawItem, Store
from trend_intelligence.timeutil import now_iso


def _seed(store: Store) -> int:
    store.ensure_source("hackernews", "community")
    return store.start_run("hackernews")


def test_migrations_apply_once_and_are_idempotent(conn: sqlite3.Connection) -> None:
    assert migrate(conn) == []
    versions = [r["version"] for r in conn.execute("SELECT version FROM schema_migrations")]
    assert versions == sorted(versions) and versions[0] == "0001_core"


def test_modified_applied_migration_is_rejected(tmp_path: Path) -> None:
    mig = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS_DIR, mig)
    c = connect(":memory:")
    migrate(c, mig)
    first = sorted(mig.glob("*.sql"))[0]
    first.write_text(first.read_text() + "\n-- edited\n")
    with pytest.raises(RuntimeError, match="modified after being applied"):
        migrate(c, mig)


def test_pre_migration_backup_when_data_exists(tmp_path: Path) -> None:
    mig = tmp_path / "migrations"
    mig.mkdir()
    shutil.copy(sorted(MIGRATIONS_DIR.glob("*.sql"))[0], mig)
    db = tmp_path / "x.db"
    c = connect(db)
    migrate(c, mig, backup_dir=tmp_path / "bk")
    assert not (tmp_path / "bk").exists()  # nothing to protect on first run
    (mig / "0099_extra.sql").write_text("CREATE TABLE extra_t (id INTEGER PRIMARY KEY);")
    migrate(c, mig, backup_dir=tmp_path / "bk")
    assert len(list((tmp_path / "bk").glob("*.db"))) == 1


def test_observations_and_raw_items_are_append_only(conn: sqlite3.Connection) -> None:
    store = Store(conn)
    run = _seed(store)
    store.insert_raw_item("hackernews", RawItem("story", "1", title="t"), run, now_iso())
    store.add_observations("hackernews", [Observation("story", "hn:1", "points", 5)], run,
                           now_iso())
    for sql in ("UPDATE observations SET value=99", "DELETE FROM observations",
                "UPDATE raw_items SET title='x'", "DELETE FROM raw_items"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(sql)


def test_raw_item_dedup_and_payload_compressed(conn: sqlite3.Connection) -> None:
    store = Store(conn)
    run = _seed(store)
    item = RawItem("story", "42", title="Hello", payload={"a": 1})
    rid, new = store.insert_raw_item("hackernews", item, run, now_iso())
    rid2, new2 = store.insert_raw_item("hackernews", RawItem("story", "42", title="changed"),
                                       run, now_iso())
    assert new and not new2 and rid == rid2
    assert conn.execute("SELECT title FROM raw_items WHERE id=?", (rid,)).fetchone()[0] == "Hello"
    blob = conn.execute("SELECT payload_gz FROM raw_payloads WHERE raw_item_id=?",
                        (rid,)).fetchone()[0]
    assert json.loads(gzip.decompress(blob)) == {"a": 1}


def test_point_in_time_reads_never_see_later_data(conn: sqlite3.Connection) -> None:
    """Future-data leakage guard: as_of reads ignore rows that became available later."""
    store = Store(conn)
    run = _seed(store)
    pts = [("2026-01-01T00:00:00.000000Z", 10.0), ("2026-01-02T00:00:00.000000Z", 50.0),
           ("2026-01-03T00:00:00.000000Z", 400.0)]
    store.add_observations(
        "hackernews",
        [Observation("story", "hn:9", "points", v, observed_at=t) for t, v in pts],
        run, now_iso(),
    )
    as_of = "2026-01-02T12:00:00.000000Z"
    before = store.value_as_of("story", "hn:9", "points", as_of)
    series_before = store.series_as_of("story", "hn:9", "points", as_of)
    # Insert even later data and a late-arriving correction for the past.
    store.add_observations("hackernews", [Observation(
        "story", "hn:9", "points", 9999, observed_at="2026-01-01T06:00:00.000000Z",
        available_at="2026-01-05T00:00:00.000000Z")], run, now_iso())
    assert before == 50.0
    assert store.value_as_of("story", "hn:9", "points", as_of) == before
    assert store.series_as_of("story", "hn:9", "points", as_of) == series_before
    assert [v for _, v in series_before] == [10.0, 50.0]


def test_live_observation_cannot_claim_future_availability(conn: sqlite3.Connection) -> None:
    store = Store(conn)
    run = _seed(store)
    with pytest.raises(ValueError, match="future"):
        store.add_observations("hackernews", [Observation(
            "story", "hn:1", "points", 1, observed_at="2099-01-01T00:00:00Z")], run, now_iso())
    with pytest.raises(ValueError, match="precede"):
        store.add_observations("hackernews", [Observation(
            "story", "hn:1", "points", 1, observed_at="2026-01-02T00:00:00Z",
            available_at="2026-01-01T00:00:00Z")], run, now_iso())


def test_duplicate_measurement_instant_is_ignored(conn: sqlite3.Connection) -> None:
    store = Store(conn)
    run = _seed(store)
    obs = Observation("story", "hn:1", "points", 3, observed_at="2026-01-01T00:00:00Z")
    assert store.add_observations("hackernews", [obs], run, now_iso()) == 1
    assert store.add_observations("hackernews", [obs], run, now_iso()) == 0


def test_backup_snapshot_is_readable(tmp_path: Path, conn: sqlite3.Connection) -> None:
    store = Store(conn)
    _seed(store)
    path = backup(conn, tmp_path / "bk", label="t")
    copy = sqlite3.connect(path)
    assert copy.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1


def test_initial_snapshot_view_marks_first_run_only(conn: sqlite3.Connection) -> None:
    store = Store(conn)
    store.ensure_source("defillama", "x")
    r1 = store.start_run("defillama")
    store.insert_raw_item("defillama", RawItem("protocol", "a"), r1, now_iso())
    r2 = store.start_run("defillama")
    store.insert_raw_item("defillama", RawItem("protocol", "b"), r2, now_iso())
    flags = dict(conn.execute("SELECT external_id, is_initial_snapshot FROM v_raw_items"
                              ).fetchall())
    assert flags == {"a": 1, "b": 0}


def test_clone_farm_view(conn: sqlite3.Connection) -> None:
    store = Store(conn)
    store.ensure_source("github", "code")
    run = store.start_run("github")
    for owner in ("u1", "u2", "u3"):
        store.insert_raw_item("github", RawItem("repo", f"{owner}/same-tool"), run, now_iso())
    store.insert_raw_item("github", RawItem("repo", "real/unique"), run, now_iso())
    rows = dict(conn.execute("SELECT external_id, suspected_clone_farm FROM v_repo_quality"
                             ).fetchall())
    assert rows["u1/same-tool"] == 1 and rows["real/unique"] == 0
