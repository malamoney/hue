# Field numbers live in a committed lock file, not in spec order

Protobuf field numbers are the wire format, and they are forever. The generator originally derived them from the order properties appear in `openapi.yaml`, which meant a single property inserted upstream would shift every number after it. Regenerating after a Hue spec update would then produce definitions that are wire-incompatible with anything already deployed — a client built against the old file would decode the new `mode` bytes as the old `signaling`, silently, with no error at either end. Numbers are therefore assigned from `proto/field-numbers.json`, keyed by the message's full nested path (`LightGet.Powerup.preset`), read before generation and written back after it. An assignment is never changed once made.

## Considered Options

- **Commit the generated `.proto` and treat it as the source of truth thereafter.** Rejected: it turns every spec update into a manual diff-and-port exercise where numbering stability depends on the reviewer noticing, and it contradicts [ADR 0001](./0001-custom-openapi-to-proto-generator.md)'s regenerate-from-spec workflow.
- **Accept order-derived numbering and document the hazard.** Rejected. It is survivable today, with one client we control and can redeploy in lockstep, but the failure is silent and arrives long after the change that caused it. A lock file costs one JSON file.

## Consequences

- `proto/field-numbers.json` is a committed artefact and must be updated in the same commit as any regeneration. A conflict in it during a merge is meaningful and must not be resolved by regenerating.
- Numbers belonging to fields that have since disappeared are retained and never reissued: allocation takes `max + 1` rather than filling the lowest gap. Reusing a retired number would make old and new clients disagree about what it means.
- Nothing yet emits `reserved` ranges for those retired numbers. The lock file prevents reuse by construction, so this is a readability gap in the generated `.proto` rather than a correctness one.
- The lock file records numbering only. It is not a schema history and cannot detect a field changing type, which remains a breaking change that CI does not currently catch.
