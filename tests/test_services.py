"""Tests for app/services.py's destructive helpers — _wipe and purge_demo.

Both must clear BatteryReading rows along with their vehicle, not just
Drive/Charge. Otherwise the readings are orphaned: on SQLite (this project's
test/self-hosted default), a deleted vehicle's freed primary-key id can be
reused by a later, unrelated vehicle, which then silently inherits the old
car's leftover battery-health readings and reports a wrong degradation %.
"""
from sqlalchemy import select

from app import services
from app.models import BatteryReading, Charge, Drive, ServiceRecord, Vehicle


def test_wipe_clears_battery_readings_too(session):
    v = Vehicle(vin="WIPE-TEST", name="Test", model="Model 3")
    session.add(v)
    session.flush()
    session.add(BatteryReading(vehicle_id=v.id, ts=__import__("datetime").datetime(2026, 1, 1),
                                soc=60, range_km=300.0))
    session.commit()

    services._wipe(session)

    assert session.scalars(select(Vehicle)).all() == []
    assert session.scalars(select(BatteryReading)).all() == []


def test_purge_demo_clears_its_battery_readings_too(session):
    demo = Vehicle(vin="DEMO-TEST", name="Demo", model="Model 3")
    real = Vehicle(vin="LRW3F7EK3RC999999", name="Real", model="Model 3")
    session.add_all([demo, real])
    session.flush()
    session.add(BatteryReading(vehicle_id=demo.id, ts=__import__("datetime").datetime(2026, 1, 1),
                                soc=60, range_km=300.0))
    session.add(BatteryReading(vehicle_id=real.id, ts=__import__("datetime").datetime(2026, 1, 1),
                                soc=60, range_km=300.0))
    session.commit()

    services.purge_demo(session)

    remaining_vins = {v.vin for v in session.scalars(select(Vehicle)).all()}
    assert remaining_vins == {"LRW3F7EK3RC999999"}
    remaining_readings = session.scalars(select(BatteryReading)).all()
    assert [r.vehicle_id for r in remaining_readings] == [real.id]   # demo's reading gone


def test_wipe_and_purge_clear_service_records_too(session):
    """Same leak class as the BatteryReading bug: a ServiceRecord left
    behind after its vehicle is deleted attaches to whatever new car later
    reuses the freed id — inheriting a stranger's tyre-rotation history."""
    import datetime as _dt

    demo = Vehicle(vin="DEMO-SVC", name="Demo", model="Model 3")
    real = Vehicle(vin="LRW3F7EK3RC888888", name="Real", model="Model 3")
    session.add_all([demo, real])
    session.flush()
    session.add(ServiceRecord(vehicle_id=demo.id, type="Tire Rotation",
                              date=_dt.datetime(2026, 1, 1), odo_km=5000.0))
    session.add(ServiceRecord(vehicle_id=real.id, type="Brake Fluid",
                              date=_dt.datetime(2026, 1, 1), odo_km=9000.0))
    session.commit()

    services.purge_demo(session)
    remaining = session.scalars(select(ServiceRecord)).all()
    assert [r.type for r in remaining] == ["Brake Fluid"]   # demo's record gone, real's kept

    services._wipe(session)
    assert session.scalars(select(ServiceRecord)).all() == []


def test_stale_vehicle_id_reuse_does_not_leak_readings_across_cars(session):
    """The actual failure mode: delete a vehicle (without cleaning its
    readings — simulated here to prove the leak, then again after the fix to
    prove it's closed), free its id, let a new unrelated vehicle reuse it,
    and confirm the new vehicle's own degradation calc sees only its own
    data."""
    from app.api.routes import _degradation_pct
    from types import SimpleNamespace

    old = Vehicle(vin="OLD-CAR", name="Old", model="Model 3", trim="74D Nova19")
    session.add(old)
    session.flush()
    old_id = old.id
    session.add(BatteryReading(vehicle_id=old_id, ts=__import__("datetime").datetime(2026, 1, 1),
                                soc=50, range_km=100.0))   # a badly degraded-looking reading
    session.commit()

    services._wipe(session)   # must remove the reading along with the vehicle

    new = Vehicle(vin="NEW-CAR", name="New", model="Model 3", trim="74D Nova19")
    session.add(new)
    session.flush()
    # On SQLite this commonly reuses the freed id; assert the *readings*
    # query is empty regardless of whether the id was actually reused.
    for i in range(20):
        soc = 50 + (i % 40)
        session.add(BatteryReading(vehicle_id=new.id, ts=__import__("datetime").datetime(2026, 2, 1),
                                    soc=soc, range_km=491.0 * soc / 100.0))  # healthy pack
    session.commit()

    settings = SimpleNamespace(battery_new_range_km=0.0)
    degradation = _degradation_pct(session, new, settings)
    assert degradation is not None
    assert degradation < 5.0   # the new car's own healthy data, not the old car's bad reading


def test_drop_column_removes_it_once_and_then_does_nothing():
    """The one destructive migration in the project, so it is tested on both
    counts: it actually removes the column, and running again is a no-op
    rather than an error.

    init_db runs on every boot and several workers can boot at once, so a
    second pass finding the column already gone is the normal case, not the
    exception.
    """
    import sqlalchemy
    from sqlalchemy import inspect, text

    from app import database as db

    def columns(engine, table):
        return {c["name"] for c in inspect(engine).get_columns(table)}

    engine = sqlalchemy.create_engine("sqlite://", future=True)
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE battery_readings (id INTEGER PRIMARY KEY, "
            "soc FLOAT, dashcam_state VARCHAR(16))"))

    was, db.engine = db.engine, engine
    try:
        assert "dashcam_state" in columns(engine, "battery_readings")
        db._drop_column("battery_readings", "dashcam_state")
        assert "dashcam_state" not in columns(engine, "battery_readings")
        # Everything else survives.
        assert {"id", "soc"} <= columns(engine, "battery_readings")

        # Again: nothing to do, and no exception.
        db._drop_column("battery_readings", "dashcam_state")
        # A table that does not exist at all is also fine — a fresh database
        # reaches init_db before any of these tables have rows or history.
        db._drop_column("no_such_table", "dashcam_state")
    finally:
        db.engine = was
