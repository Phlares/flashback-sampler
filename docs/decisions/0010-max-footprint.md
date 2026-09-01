# 0010. Max footprint: 25 % of RAM by default, tunable, 0 = no cap

Date: 2026-09-01. Status: accepted.

## Context

Arming a slot commits its whole ring up front. A fixed 4 GB stop refused a fifth slot on a 128 GB machine and let a 4 GB slot through on an 8 GB one. The number had no basis.

## Decision

- Safe defaults plus tunables. The max footprint defaults to 25 % of physical RAM, read at launch from the engine (`fb_mem_info`). A stored preference overrides it. 0 means no cap.
- Two checks run before a ring is created. Over the footprint when a cap is set: refuse, name the total and the cap. A ring larger than free physical memory: refuse, name both numbers. A platform that cannot report memory skips the free-memory check.
- The engine's own `out_of_memory` at ring creation stays as the backstop.
- The footprint is a safety line, not a reservation. Mobile shells inherit the same policy; app stores now track resident memory per device class.

## Consequences

- Whoever wants 16 GB of ring on a 256 GB box sets it. Whoever wants none of this sets 0.
- The check reads one number from the OS per arm. No accounting thread.
