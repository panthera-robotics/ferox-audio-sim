from types import SimpleNamespace

from ferox_audio_g1.diagnostic_contract import (
    BOOLEANS,
    COUNTERS,
    KEYS,
    TIMINGS,
    validate_audio_diagnostic,
)


def _message(*, level=0, changes=None):
    values = {key: "0" for key in COUNTERS}
    values.update({key: "false" for key in BOOLEANS})
    values.update({
        "schema_version": "2",
        "ready": "true" if level == 0 else "false",
        "last_fault": "",
    })
    values.update({key: ("0.0" if key == "buffered_audio_ms" else "-1.0")
                   for key in TIMINGS})
    values.update(changes or {})
    assert set(values) == set(KEYS)
    status = SimpleNamespace(
        level=level,
        name="ferox/g1_01/audio",
        hardware_id="g1_01",
        message="healthy" if level == 0 else "degraded",
        values=[SimpleNamespace(key=key, value=value)
                for key, value in values.items()],
    )
    return SimpleNamespace(status=[status])


def test_exact_audio_diagnostic_schema_is_accepted():
    assert validate_audio_diagnostic(_message(), robot_id="g1_01") is None
    wire_shape = _message()
    wire_shape.status[0].level = b"\x00"
    assert validate_audio_diagnostic(wire_shape, robot_id="g1_01") is None


def test_spoofed_malformed_and_contradictory_statuses_are_rejected():
    spoofed = _message()
    spoofed.status[0].hardware_id = "other"
    assert "hardware id" in validate_audio_diagnostic(
        spoofed, robot_id="g1_01")

    unknown = _message()
    unknown.status[0].values[-1].key = "unreviewed"
    assert "key" in validate_audio_diagnostic(unknown, robot_id="g1_01")

    assert "outside bounds" in validate_audio_diagnostic(
        _message(changes={"inflight_age_ms": "nan"}), robot_id="g1_01")
    assert "contradicts" in validate_audio_diagnostic(
        _message(level=2, changes={"ready": "true"}), robot_id="g1_01")
