"""Go2 has no production in-path acoustic echo canceller. Fail-closed stub.

The live bridge only publishes/subscribes PCM and does not run a canceller.
The separate native WebRTC AEC3 offline tool produces bounded engineering
evidence, but it is not connected to the robot acoustic loop and cannot satisfy
ETSI ES 202 738 TCLw or ITU-T P.340 TELRDT. Calling this module never enables
the speaker.
"""
from __future__ import annotations

from collections.abc import Mapping

POLICY_ID = "ferox-audio-world-class-v1"

AEC_GATES = (
    {
        "gate_id": "aec_tclw_db",
        "metric": "tclw_db",
        "threshold": 46.0,
        "published_bar": 46.0,
        "corpus": "ETSI ES 202 738 TCLw, ITU-T G.122 trapezoidal, P.501 speech",
        "standard": "ES 202 738",
    },
    {
        "gate_id": "aec_p340_telrdt_db",
        "metric": "telrdt_db",
        "threshold": 37.0,
        "published_bar": 37.0,
        "corpus": "ITU-T P.340 Type 1 double-talk TELRDT",
        "standard": "P.340",
    },
    {
        "gate_id": "aec_far_end_erle_db",
        "metric": "erle_db",
        "threshold": 20.0,
        "published_bar": 20.0,
        "corpus": "far-end single talk; engineering ERLE, not TCLw",
        "standard": "G.167 engineering floor only",
    },
)


class AecUnavailableError(ValueError):
    """Raised when a caller treats Go2 PCM or ERLE as a canceller/TCLw result."""


def aec_unavailable(evidence: Mapping[str, object] | None = None) -> dict[str, object]:
    """Return the fail-closed AEC interface. Always missing_measurement.

    Injected tclw_db / erle_db / hats_campaign values are ignored: this
    live adapter has no in-path canceller to measure. speaker_enable_authorized stays
    false. production_ready stays false.
    """
    del evidence
    gates = []
    for spec in AEC_GATES:
        gates.append({
            **spec,
            "measured": None,
            "reason": "missing_measurement",
            "status": "fail",
            "passed": False,
            "canceller_present": False,
            "tclw_authorized": False,
        })
    return {
        "policy_id": POLICY_ID,
        "interface": "aec_unavailable",
        "evidence_class": "ferox_audio_go2_aec_absent",
        "canceller_present": False,
        "canceller_module": None,
        "control_authorized": False,
        "mic_enable_authorized": False,
        "speaker_enable_authorized": False,
        "production_ready": False,
        "tclw_authorized": False,
        "p340_authorized": False,
        "erle_is_not_tclw": True,
        "passed": False,
        "gates": gates,
        "reason": (
            "ferox-audio-go2 has no production in-path AEC module; aec_tclw_db, "
            "aec_p340_telrdt_db, and aec_far_end_erle_db remain "
            "missing_measurement; speaker_enable_authorized=false; "
            "engineering ERLE is not ETSI TCLw"
        ),
    }


def refuse_engineering_erle_as_tclw(*_args, **_kwargs) -> None:
    """Hard stop: do not compute ERLE here and do not label it TCLw."""
    raise AecUnavailableError(
        "ferox-audio-go2 has no production in-path canceller; offline engineering ERLE is "
        "not ETSI ES 202 738 / TS 103 738 TCLw and never authorize the speaker"
    )


def main(args=None) -> None:
    del args
    import json

    print(json.dumps(aec_unavailable(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
