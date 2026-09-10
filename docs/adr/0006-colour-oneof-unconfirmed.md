# Model LightPut colour and colour temperature as a oneof, on an unconfirmed premise

**Status:** accepted; the premise is unverified against real hardware — see [#35](https://github.com/malamoney/hue/issues/35).

`proto/manifest.toml` tells the generator to wrap `LightPut.color` and `LightPut.color_temperature` in a `oneof`, so a client cannot set both in one `UpdateLight`. Hue's documentation says the Bridge rejects a request that carries both, and a `oneof` turns "the gateway validates and returns `INVALID_ARGUMENT`" into "the request cannot be constructed" — the same move as [ADR 0001](./0001-custom-openapi-to-proto-generator.md)'s field-presence work, one class of wrong request designed out rather than checked for.

The premise is only documentation. The OpenHue spec has zero `oneOf` ([ADR 0001](./0001-custom-openapi-to-proto-generator.md)), so this pairing is hand-annotated in the manifest, and no test has sent both fields to a real Bridge. The manifest comment originally said "verify in #16"; issue #16 (deploy and smoke-test) closed on 2026-09-10 having only exercised the brightness write path, so the check never happened. It is now [#35](https://github.com/malamoney/hue/issues/35).

## Considered Options

- **Two `optional` fields, validate and reject at the gateway.** The always-safe choice: whatever the Bridge does, the gateway can match it. Rejected for now because it gives up the property that a malformed request is unrepresentable — a client that sets both gets a runtime error instead of a compile-time one — for a rule Hue itself documents. If [#35](https://github.com/malamoney/hue/issues/35) shows the Bridge accepts both, this is what the `oneof` becomes.
- **Leave it a `oneof` and say nothing.** Rejected: the decision is baked into the wire shape of `LightPut`, and unwinding it after clients exist changes the generated message (the field numbers survive under [ADR 0003](./0003-committed-field-number-lock.md), the `oneof` wrapper does not). A commitment that size needs a record and a tracked way to confirm it, not a TOML comment pointing at a closed issue.

## Consequences

- `tests/smoke/test_live_lights.py` carries the verification: a `HUE_CHANGE_LIGHTS=1` test that sends both fields at the light's *current* values over the raw `HueTransport` — bypassing the `oneof`, which makes the request impossible through the Gateway — and asserts the Bridge refuses it. Current values mean a Bridge that wrongly accepts the request changes nothing visible, but the assertion still fails and says why.
- Until that test has run against hardware, treat the `oneof` as load-bearing but provisional. It is already depended on by `tests/protogen/` and `tests/unit/test_codec.py`.
- If the Bridge rejects both: update the manifest comment and this file to "confirmed" and close [#35](https://github.com/malamoney/hue/issues/35).
- If the Bridge accepts both: replace the `oneof` with two `optional` fields, add the range/exclusivity check to `codec.py`, and supersede this ADR.
