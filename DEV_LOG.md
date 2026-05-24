# Dev Log

Append-only log of repo-level changes that aren't obvious from `git log`
alone — conventions, recurring footguns, behavior changes. Keep entries
terse; link to PRs / files for detail.

---

## 2026-05-24 — Env-driven Cyclone DDS

Aligned cyclone DDS with the Panthera-wide env-driven pattern
(Ferox 1.1, ferox-speech 1.2, ferox-isaac-demo 1.3).

**Two optional env vars** replace the prior required-pair:
- `FEROX_DDS_INTERFACE` — pin Cyclone to this interface (e.g. `tailscale0`).
  Empty/unset => auto-detect.
- `FEROX_DDS_PEERS` — space-separated peer IPs for cross-network discovery.
  Empty/unset => multicast-only on local LAN.

**Behavior change:** `scripts/start.sh` no longer fail-louds on
`FEROX_DDS_PEER_HOST` / `FEROX_DDS_PEER_CLOUD` (which are gone). Empty is
now valid and means "host-on-LAN, multicast discovery."

**No image change:** the Dockerfile already had `ENV CYCLONEDDS_URI=...`
and the chained DDS entrypoint. Only the template + entrypoint script
contents changed.

See `.env.example` for setup scenarios.
