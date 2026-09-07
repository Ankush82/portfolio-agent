"""STORY-7: get_audit_reader() exposes AuditReader end-to-end.

Verifies the three acceptance criteria:
  1. Infrastructure Protocol includes get_audit_reader() -> AuditReader.
  2. PostgresInfrastructure.get_audit_reader() returns a DefaultAuditReader.
  3. infrastructure.get_audit_reader().query(...) works end-to-end
     against the real `queue_events` table.
"""

from __future__ import annotations

from infrastructure import (
    AuditReader,
    Infrastructure,
    StubAuditReader,
    StubInfrastructure,
)
from infrastructure_postgres import (
    DEFAULT_POSTGRES_DSN,
    DefaultAuditReader,
    DefaultInfrastructure,
)


def test_infrastructure_protocol_exposes_get_audit_reader():
    """Protocol-level check: AuditReader and get_audit_reader are
    part of the interface surface every component is allowed to
    depend on."""
    # Protocol members show up via dir(); if get_audit_reader isn't
    # declared on Infrastructure this assertion fails.
    assert "get_audit_reader" in dir(Infrastructure)
    # AuditReader is the return type contract.
    assert AuditReader is not None


def test_stub_infrastructure_returns_stub_audit_reader():
    """Stub parity: StubInfrastructure.get_audit_reader() must return
    something AuditReader-shaped that behaves as a no-op reader."""
    reader = StubInfrastructure().get_audit_reader()
    assert isinstance(reader, StubAuditReader)
    # No-op read returns an empty list, never raises.
    assert reader.query() == []
    assert reader.query(topic="any.topic") == []


def test_postgres_audit_reader_is_default_audit_reader_instance():
    """Postgres implementation returns a DefaultAuditReader bound to
    the same infrastructure that produced it."""
    infra = DefaultInfrastructure(postgres_dsn=DEFAULT_POSTGRES_DSN)
    reader = infra.get_audit_reader()
    assert isinstance(reader, DefaultAuditReader)
    # Bound to this infra, not some other one.
    assert reader._infrastructure is infra


def test_audit_reader_query_end_to_end():
    """End-to-end: publish two events, then read them back through
    the AuditReader. Confirms the reader sees exactly what publish()
    wrote, on the requested topic, newest-first."""
    import uuid

    infra = DefaultInfrastructure(postgres_dsn=DEFAULT_POSTGRES_DSN)
    reader = infra.get_audit_reader()

    topic = f"story7.test.{uuid.uuid4().hex}"
    marker_a = {"marker": uuid.uuid4().hex}
    marker_b = {"marker": uuid.uuid4().hex}

    infra.publish(topic, marker_a)
    infra.publish(topic, marker_b)

    # Newest-first: marker_b should come back before marker_a.
    rows = reader.query(topic=topic)
    assert len(rows) >= 2
    # The two we just published must be in the result, newest-first.
    recent = rows[:2]
    assert recent[0]["topic"] == topic
    assert recent[0]["event"] == marker_b
    assert recent[1]["topic"] == topic
    assert recent[1]["event"] == marker_a

    # Shape contract from the Protocol docstring.
    for row in recent:
        assert set(row.keys()) == {"id", "topic", "event", "published_at", "consumed"}
        assert isinstance(row["id"], int)
        assert isinstance(row["topic"], str)
        assert isinstance(row["event"], dict)
        assert isinstance(row["published_at"], str)
        assert isinstance(row["consumed"], bool)


def test_audit_reader_topic_filter_isolates_results():
    """Topic filter excludes other topics — proves the WHERE clause
    is actually wired up, not silently returning all rows."""
    import uuid

    infra = DefaultInfrastructure(postgres_dsn=DEFAULT_POSTGRES_DSN)
    reader = infra.get_audit_reader()

    topic = f"story7.isolated.{uuid.uuid4().hex}"
    other_topic = f"story7.other.{uuid.uuid4().hex}"
    marker = {"marker": uuid.uuid4().hex}

    infra.publish(topic, marker)
    infra.publish(other_topic, {"marker": "should-not-appear"})

    rows = reader.query(topic=topic)
    assert all(row["topic"] == topic for row in rows)
    assert any(row["event"] == marker for row in rows)


def test_audit_reader_limit_caps_returned_rows():
    """limit parameter caps the number of rows returned."""
    import uuid

    infra = DefaultInfrastructure(postgres_dsn=DEFAULT_POSTGRES_DSN)
    reader = infra.get_audit_reader()

    topic = f"story7.limit.{uuid.uuid4().hex}"
    # Publish 3, ask for 2.
    for i in range(3):
        infra.publish(topic, {"i": i})

    rows = reader.query(topic=topic, limit=2)
    assert len(rows) == 2


def test_audit_reader_since_until_filters_window_end_to_end():
    """Story-specific (STORY-7): end-to-end verification that the
    AuditReader returned by infrastructure.get_audit_reader() honours
    the documented `since`/`until` filters against the REAL
    queue_events table. This is the only test that exercises the
    since/until half of the protocol contract end-to-end through
    the Infrastructure interface — the other tests in this file only
    cover topic + limit.

    Strategy: publish a marker, then read it back WITHOUT a time
    window to discover the marker's actual `published_at` value as
    Postgres recorded it (parses the ISO-8601 string the reader
    returned). Then build an `until` bound that is strictly earlier
    than that value, and assert the same query with the until bound
    returns ZERO rows. This sidesteps the real Postgres-vs-Python
    clock-skew issue: we never compare a Python-side wall-clock
    timestamp to Postgres's own `now()`-resolved one — we use
    Postgres's own recorded value as the reference."""
    from datetime import datetime, timedelta, timezone
    import uuid

    infra = DefaultInfrastructure(postgres_dsn=DEFAULT_POSTGRES_DSN)
    reader = infra.get_audit_reader()

    topic = f"story7.window.{uuid.uuid4().hex}"
    marker = {"marker": f"unique-{uuid.uuid4().hex}"}
    since = datetime.now(timezone.utc) - timedelta(hours=1)

    # Publish the marker.
    infra.publish(topic, marker)

    # Read it back WITHOUT a time window to discover its real
    # published_at as Postgres recorded it.
    unbounded = reader.query(topic=topic, since=since, limit=100)
    assert any(r["event"] == marker for r in unbounded), (
        f"sanity check failed: topic filter didn't return the marker; "
        f"got {unbounded}"
    )
    marker_row = next(r for r in unbounded if r["event"] == marker)
    # Parse the ISO-8601 string the reader returned.
    marker_published_at = datetime.fromisoformat(
        marker_row["published_at"].replace("Z", "+00:00")
    )

    # Build `until` strictly EARLIER than the marker's actual
    # published_at, so the marker MUST be excluded by the until
    # filter (published_at <= until is false).
    until_excluding_marker = marker_published_at - timedelta(microseconds=1)

    rows = reader.query(
        topic=topic, since=since, until=until_excluding_marker, limit=100
    )
    assert rows == [], (
        f"until filter did not exclude rows strictly before its bound; "
        f"marker_published_at={marker_published_at}, "
        f"until={until_excluding_marker}, got {rows}"
    )

    # And the same row IS returned when `until` is set to the
    # marker's actual published_at (inclusive bound, per protocol
    # docstring: "until: inclusive upper bound").
    rows_inclusive = reader.query(
        topic=topic, since=since, until=marker_published_at, limit=100
    )
    assert any(r["event"] == marker for r in rows_inclusive), (
        f"inclusive until bound dropped the marker; "
        f"marker_published_at={marker_published_at}, got {rows_inclusive}"
    )


def test_audit_reader_returned_object_conforms_to_audit_reader_protocol():
    """Story-specific (STORY-7): the object returned by
    infrastructure.get_audit_reader() must expose a callable .query(...)
    with the exact signature the AuditReader Protocol declares
    (topic, since, until, limit — all keyword-argumentable). If dev's
    change returned e.g. a raw psycopg cursor, or a class that renamed
    query to read/get, runtime duck-typing would still work in some
    places but every other component depending on the AuditReader
    boundary would silently break. This test pins that contract."""
    from datetime import datetime
    import inspect

    infra = DefaultInfrastructure(postgres_dsn=DEFAULT_POSTGRES_DSN)
    reader = infra.get_audit_reader()

    # Must have a callable .query — the only method the Protocol requires.
    assert hasattr(reader, "query"), "AuditReader-shaped object has no .query method"
    assert callable(reader.query)

    # The Protocol's declared query() signature: (self, topic=None,
    # since=None, until=None, limit=100). The concrete class's query
    # must accept the same keyword arguments.
    sig = inspect.signature(reader.query)
    param_names = list(sig.parameters.keys())
    assert "topic" in param_names, (
        f"reader.query missing 'topic' parameter; got {param_names}"
    )
    assert "since" in param_names, (
        f"reader.query missing 'since' parameter; got {param_names}"
    )
    assert "until" in param_names, (
        f"reader.query missing 'until' parameter; got {param_names}"
    )
    assert "limit" in param_names, (
        f"reader.query missing 'limit' parameter; got {param_names}"
    )

    # And calling it through the Infrastructure boundary end-to-end
    # with all four parameters must not raise and must return a list.
    rows = reader.query(
        topic=None,
        since=datetime.fromisoformat("2000-01-01T00:00:00+00:00"),
        until=datetime.fromisoformat("2100-01-01T00:00:00+00:00"),
        limit=5,
    )
    assert isinstance(rows, list)
    assert len(rows) <= 5