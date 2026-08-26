# HATS + 1 m spoken runbook (later)

**Status: recipe only. Do not execute this capture now.**

`production_ready=false`. `speaker_enable_authorized=false`. `control_authorized=false`.
`ferox-audio-go2` has **no AEC module**. AEC gates stay `missing_measurement`.
Engineering ERLE is **not** ETSI TCLw. This document does not authorize
speaker unmute, G1 PlayStream, AudioHub, TTS playback, or a live campaign.

Hard holds (still in force):

- Do **not** unmute the robot speaker.
- Do **not** call G1 `1001` / `1003` / `1006` PlayStream.
- Do **not** call AudioHub `4001` / `4003`.
- Do **not** start a live 1 m spoken campaign that plays TTS.
- Do **not** claim TCLw.
- Do **not** kill Spark GPU.

---

## Why this exists

Two different missing measurements are often collapsed into one “audio FAIL”:

| Item | What we have | What it is **not** |
| --- | --- | --- |
| Empty-room 180 s Go2 WAV | Idle `/audiosender` transport decode | WER, intelligibility, AEC |
| `ferox-speech` `engineering_erle_db` | Time-domain energy ratio helper | ETSI ES 202 738 / TS 103 738 TCLw |
| `ferox_audio_go2.aec_unavailable` | Fail-closed stub | A canceller |

The 180 s custody WAV:

- path: `ni/outputs/go2-180s-decode-20260819T1829Z/reliable-opus48.wav`
- `recording_sha256`: `388b4e31942772ddb248d31576fe3191aa1f6126553a34882ea1f6e89273662e`
- capture_sha256: `4634787eff4bd275bc82c98f3673c5a3a2c63ff0ca162b0f66e56908ce020638`
- listen 2026-08-20, **headphones**, speaker still off: **not intelligible** (stationary idle noise / faint HVAC)
- `operator_audio_intelligible` remains **false**
- empty hypothesis is **not** a WER

**Verdict: idle-noise FAIL.** Do not remux this file into a WER or TCLw claim. The next mic capture must contain **live speech at 1 m**, then a headphones listen of **that** decode.

---

## Phase A — 1 m spoken mic capture (speaker stays off)

Run this later, with an operator present. **Speaker remains off.** No TTS, no AudioHub, no PlayStream.

Preconditions:

- Robot idle, no motion.
- Talker at **1 m** on-axis to the Go2 mic (also plan 0.5 m and 2 m later).
- Known phrases, EN + AR + HI, ≥50 utterances / language, ≥5 speakers, ≥200 utterances total (world-class live WER policy).
- Read-only `/audiosender` subscribe + decode (`go2_opus48_audiohub_v1`). Same path as the 180 s capture.
- Operator listens on **headphones** to the **new** WAV. Only then may `operator_id` and `operator_audio_intelligible` be filled.
- `speaker_probe` stays **null**. `GO2_AUDIO_SPEAKER_ENABLED` stays false.

Pass for this phase is **intelligibility + live WER**, not AEC. Empty-room `388b4e31…` cannot pass it.

Do **not** start this phase from the 2026-08-20T21:34Z session.

---

## Phase B — HATS AEC (requires human speaker authorization)

Standards (primary, not blogs):

- [ETSI ES 202 738 V1.8.2](https://www.etsi.org/deliver/etsi_es/202700_202799/202738/01.08.02_60/es_202738v010802p.pdf) — TCLw (weighted terminal coupling loss)
- [ETSI TS 103 738](https://www.etsi.org/deliver/etsi_TS/103700_103799/103738/01.04.01_60/ts_103738v010401p.pdf) — test implementation / HATS procedure
- [ITU-T G.122](https://www.itu.int/rec/T-REC-G.122/en) trapezoidal weighting
- [ITU-T P.581 (07/2022)](https://www.itu.int/rec/T-REC-P.581/en) HATS positioning and calibration
- [ITU-T P.501 (04/2025)](https://www.itu.int/rec/T-REC-P.501/en) speech
- [ITU-T P.340](https://www.itu.int/rec/T-REC-P.340/en) Type 1 double-talk TELRDT

Bars (fail-closed; missing measurement = fail):

| Gate | Bar | Notes |
| --- | --- | --- |
| `aec_tclw_db` | ≥ 46 dB at every volume setting (recommended 50 dB) | ES 202 738 TCLw. **Not ERLE.** |
| `aec_p340_telrdt_db` | ≥ 37 dB | P.340 Type 1 |
| `aec_far_end_erle_db` | ≥ 20 dB | Engineering floor only; never qualifies duplex |
| Scenarios | far-end single talk, near-end single talk, double talk | ≥3 events and ≥30 s each |
| Barge-in | p95 onset-to-mute ≤ 200 ms | Only after AEC already passes |
| Speaker STI / STOI / POLQA | ≥ 0.75 / ≥ 0.75 / MOS ≥ 4.0 | IEC 60268-16 / P.863; PESQ P.862 withdrawn |

Lab:

- Calibrated HATS at the specified mouth/ear positions, with horizontal positioning error within ±2° (P.581 / TS 103 738).
- Three scenarios above. Do not substitute “play a WAV in an empty room.”
- **Speaker still requires a human in the loop:** supervised safe volume, operator heard the test phrase, no delayed replay for 10 s. Until those flags are true, do not run `go2_audio_speaker_probe`, do not set `GO2_AUDIO_SPEAKER_ENABLED=true`, do not publish AudioHub 4001/4003.
- Implement a **real canceller** in software/firmware **before** scoring TCLw. `aec_unavailable` will keep reporting `missing_measurement` until a canceller exists. Measuring idle PCM against an ERLE script does not create one.

Do **not** start Phase B from this session. Do not claim TCLw from any current WAV.

---

## What this session did **not** do

- Did not unmute speaker.
- Did not call G1 1001/1003/1006 or AudioHub 4001/4003.
- Did not start the 1 m spoken campaign.
- Did not play TTS.
- Did not kill Spark GPU.
- Did not set `production_ready` or `operator_audio_intelligible`.

When a later session runs Phase A or B, copy new hashes into a **new** evidence directory. Do not overwrite `388b4e31…`.
