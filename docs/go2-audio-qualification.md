# Go2 audio qualification runbook

This runbook is the hardware gate for FRX-PLAN-001 M5.1. A software test pass
does not qualify a Go2 microphone codec or speaker protocol. Run it separately
for every robot ID and firmware fingerprint; do not copy Go2 #1 evidence onto
Go2 #2.

## 1. Record the immutable target identity

With the robot stationary and an operator present, record:

- `robot_id` (`go2_01`, `go2_02`, ...)
- exact firmware string from the Unitree App
- adapter image digest
- `uname -r`, JetPack/L4T version, and `FEROX_DDS_INTERFACE`
- UTC start time and operator ID

Do not enable a speaker or any motion stack in this phase.

## 2. Read-only DDS observation

On robot domain 0, pinned to the robot LAN interface:

```bash
export ROBOT_ID=go2_02
export FEROX_DDS_INTERFACE=eth0
export GO2_AUDIO_DISCOVERY_SECONDS=15
scripts/go2_audio_discover.sh \
  /absolute/new/go2-02-audio-observation.json \
  /absolute/new/go2-02-audio-frames.jsonl
```

Pass requires at least 10 seconds and 400 non-empty frames, monotonically
increasing `AudioData.time_frame`, and a p50 cadence within 14–26 ms. The probe
does not infer a codec.

## 3. Codec discrimination

Capture the original framed payload bytes without alteration. Decode the same
capture under every candidate profile:

- `go2_opus48_audiohub_v1`: Opus, 48 kHz mono, 960 samples per 20 ms frame.
- `go2_ulaw8_mic_only`: G.711 u-law, 8 kHz mono, 160 samples per 20 ms frame.

Reject a candidate on any decoder error, wrong decoded sample count, or
unintelligible output. The operator must listen to a controlled phrase and
record the decoded WAV SHA-256, exact capture-file SHA-256, decoder, decoded
frame count/rate/channels, and their ID in the evidence manifest. Validation
requires the codec probe's capture hash to match the discovery observation;
`framed_payload_sha256` additionally identifies the source payload sequence.
Payload size alone never selects a profile.

Decode each candidate into a separate new file:

```bash
ros2 run ferox_audio_go2 go2_audio_decode_capture \
  --capture /absolute/new/go2-02-audio-frames.jsonl \
  --profile go2_opus48_audiohub_v1 \
  --wav-output /absolute/new/go2-02-opus.wav \
  --probe-output /absolute/new/go2-02-opus-probe.json
```

The generated probe deliberately leaves `operator_audio_intelligible=false`.
Only the operator who listened to the controlled phrase may change it and add
their ID before copying the object into the reviewed evidence manifest.

## 4. Microphone-only launch

Complete the evidence manifest with `speaker_probe: null`, hash the exact JSON,
and launch with `GO2_AUDIO_MIC_ENABLED=true` and speaker disabled. Verify:

```bash
ros2 topic info -v /ferox/go2_02/audio/mic_raw
timeout 10 ros2 topic hz /ferox/go2_02/audio/mic_raw
ros2 topic echo --once /ferox/go2_02/audio/diagnostics
```

Pass requires BEST_EFFORT `AudioChunk`, approximately 10 Hz, 16000 Hz mono
S16_LE chunks, `mic_stream_live=true`, zero source rejections, and no latched
fault. Save topic/type/QoS/rate and diagnostics output as evidence.

## 5. Bounded speaker probe

Only profile `go2_opus48_audiohub_v1` can enter this phase. Use a reviewed
≤2-second, 22050 Hz mono S16_LE WAV whose SHA-256 is known. Confirm a physical
speaker volume safe for the venue. First create a new canonical WAV; this is an
offline command that imports no ROS and publishes nothing:

```bash
ros2 run ferox_audio_go2 go2_audio_prepare_speaker_probe \
  --wav /absolute/reviewed/source.wav \
  --output /absolute/new/go2-speaker-probe.canonical.wav
sha256sum /absolute/new/go2-speaker-probe.canonical.wav
```

Listen to and review that exact canonical file, then run exactly one upload:

```bash
ros2 run ferox_audio_go2 go2_audio_speaker_probe \
  --wav /absolute/new/go2-speaker-probe.canonical.wav \
  --expected-on-wire-sha256 <sha256> \
  --output /absolute/new/go2-02-speaker-probe.json \
  --robot-id go2_02 \
  --runtime-firmware <exact-firmware> \
  --operator-id <operator-id> \
  --confirm-supervised-safe-volume
```

Run this inside the pinned Go2 container with `ROS_DOMAIN_ID=0`,
`RMW_IMPLEMENTATION=rmw_cyclonedds_cpp`, `FEROX_DDS_INTERFACE=eth0`, and the
rendered `CYCLONEDDS_URI`. The probe refuses hardware output if any is absent.

The command waits for an audiohub subscriber, requires identity and API ID
matching plus status zero for every 4001/4003 response, and observes for 10
seconds after completion. Its four human confirmation fields intentionally
remain `false`. Only the operator who heard the test may set them true after
confirming the following:

- request sequence starts with API 4001 and continues only with API 4003
- fixed topics are `/api/audiohub/request` and `/api/audiohub/response`
- every response identity matches and every status is zero
- operator heard the exact phrase cleanly
- no repeat, truncation, or delayed replay occurs for 10 seconds afterward

Copy the reviewed `speaker_probe` object into the manifest, then re-hash the
manifest. Only then set `GO2_AUDIO_SPEAKER_ENABLED=true`.

## 6. M5.1 loopback acceptance

Run ten controlled phrase loops with the speech mic gate active. Collect:

- source frame/rejection/discontinuity counters
- PCM `AudioChunk` rate and exact format
- `audiohub` response and completed-upload counters
- physical speaker onset measured independently of ROS publication time
- clipping, underrun, overrun, repeat, and echo observations

M5.1 remains incomplete until microphone publication and physical speaker
playback both pass on hardware. M5.2–M5.4 then require the separate 20-turn
latency/intent, venue-acoustics, and Arabic/English runs from FRX-PLAN-001.

## 7. HATS + 1 m spoken (later; do not run from this document tonight)

`ferox-audio-go2` has **no AEC canceller**. AEC gates stay
`missing_measurement`. `speaker_enable_authorized=false`. Empty-room 180 s
WAV `388b4e31…` is idle noise, **not WER**. Do not unmute the speaker, do
not call AudioHub 4001/4003 or G1 PlayStream 1001/1003/1006, and do not
start a live 1 m spoken campaign from this qualification pass.

The later procedure lives in:

- `docs/hats-1m-spoken-runbook.md` in this repo
- `ni/outputs/aec-hats-runbook-20260820T2134Z/` (evidence copy + STATUS.md)
