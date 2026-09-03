import pytest

from ferox_audio_go2.audiohub_transaction import (
    AudioHubTransaction,
    AudioHubTransactionError,
)
from ferox_audio_go2.speaker_protocol import build_audiohub_plan


def plan():
    return build_audiohub_plan(bytes(60_000), stream_id="tts-a")


def test_serializes_every_audiohub_request_behind_exact_ack():
    transaction = AudioHubTransaction(identity_seed=100)
    transaction.submit(plan())
    seen = []
    now = 1.0
    while transaction.busy:
        pending = transaction.dispatch_next(now)
        assert pending is not None
        seen.append(pending.api_id)
        assert transaction.dispatch_next(now) is None
        transaction.acknowledge(
            identity=pending.identity + 1, api_id=pending.api_id, status=0)
        assert transaction.inflight_identity == pending.identity
        transaction.acknowledge(
            identity=pending.identity, api_id=pending.api_id, status=0)
        now += 0.1
    assert seen[0] == 4001
    assert all(api_id == 4003 for api_id in seen[1:])
    assert transaction.completed_total == 1
    assert transaction.responses_ok_total == len(seen)


def test_error_or_timeout_latches_until_process_restart():
    transaction = AudioHubTransaction(timeout_s=0.5)
    transaction.submit(plan())
    pending = transaction.dispatch_next(1.0)
    transaction.acknowledge(
        identity=pending.identity, api_id=pending.api_id, status=3104)
    assert "3104" in transaction.latched_fault
    with pytest.raises(AudioHubTransactionError, match="latched"):
        transaction.submit(plan())

    transaction = AudioHubTransaction(timeout_s=0.5)
    transaction.submit(plan())
    transaction.dispatch_next(1.0)
    transaction.check_timeout(1.6)
    assert "timed out" in transaction.latched_fault


def test_matching_identity_with_wrong_api_id_latches():
    transaction = AudioHubTransaction()
    transaction.submit(plan())
    pending = transaction.dispatch_next(1.0)
    transaction.acknowledge(
        identity=pending.identity, api_id=pending.api_id + 2, status=0)
    assert "did not match" in transaction.latched_fault
