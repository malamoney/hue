# Generate .proto from OpenAPI with a purpose-built generator, not openapi-generator

We generate our `.proto` files from OpenHue's `openapi.yaml` rather than hand-writing them, but we do it with a small generator we own (~150 lines) instead of `openapi-generator`'s `protobuf-schema` target. A generic generator cannot express proto3 field presence: OpenAPI's `required` list does not map to `optional`, so every scalar is emitted bare, and `LightPut.on.on` as a plain `bool` makes "leave the power alone" and "turn the light off" identical on the wire. That is the top correctness risk for a service whose whole job is mutating lights. Owning the generator lets us emit `optional` on every non-required field and hand-annotate the mutual exclusivity Hue enforces in prose.

## Considered Options

- **`openapi-generator`, `protobuf-schema` target.** Rejected. Besides the presence problem, the spec contains 175 `allOf` and zero `oneOf`, so composition arrives as flattening decisions we don't control, and there is nothing for the tool to map onto protobuf `oneof` — Hue's "set `color` or `color_temperature`, not both" is documented only in prose. It also needs a JRE, which is solvable in Nix but adds a heavyweight dependency to the build for output we would then have to correct by hand.
- **Hand-writing the `.proto` files.** Rejected: it discards the spec as a source of truth and has to be redone by hand whenever Hue's schema moves.

## Consequences

- Codegen runs inside the Nix build and the dev shell. Generated Python is gitignored, so schema drift is structurally impossible rather than something CI has to detect.
- The generator only has to handle the constructs our supported subset actually uses. Widening the subset may require extending it — that is the accepted cost of owning it.
- The spec is OpenHue's, not Philips', so it is a seed and not authoritative. Field-level surprises get fixed against the real Bridge, in the generator, not by editing generated output.
