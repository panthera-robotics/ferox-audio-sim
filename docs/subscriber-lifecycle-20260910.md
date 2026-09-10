# Subscriber lifecycle candidate — not production-qualified

Local regressions reproduced two failures on parent 2e88e32: late join and
same-count endpoint replacement both rejected forever with `first accepted
chunk must carry FLAG_START`. The adapter now checks endpoint GIDs at most
10 Hz and cuts buffered output on a newly observed endpoint. Source timestamp
validation and decoder history remain intact. Departure alone does not cut
audio. A join deliberately cuts an existing listener's utterance; this is a
continuity boundary, not permission to dispatch a partial command.

Run after committing, using a Python 3.10–3.12 environment with project test
dependencies (no robot/network/container work is performed by this script):

```sh
python scripts/qualify_offline_revision.py --expected-revision FULL_COMMIT_SHA
```

The JSON receipt binds tracked files before/after tests and explicitly excludes
live and speech-quality qualification. libopus availability must be checked in
pytest output; a skipped codec test is not codec acceptance.

Parent-owned live acceptance: exact-SHA image, approved evidence file/hash and
firmware identity, mic-only and speaker disabled, isolated application domain;
join a second metadata listener, then replace it while retaining the first.
Require each newly discovered GID to receive START|DISCONTINUITY with zero
counters, fresh timestamps, strict format, and no mixed stream. Repeat complete
disconnect/rejoin. Record graph-discovery delay, CPU and receive gaps (graph
query overhead is not yet target-qualified). Repeat SIGTERM and require clean
exit plus retained post-stop logs. No playback, nav, key or lease changes.

Known boundary: graph visibility is not an acknowledgement that DDS delivered
START. BEST_EFFORT loss of that boundary can still leave a strict consumer
closed until another explicit boundary. Do not relax guards to conceal this.
Meaningful near-field EN/AR speech, acoustic latency, AEC and WER remain open;
ambient Opus decoding cannot close them.
