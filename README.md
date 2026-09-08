# hue-grpc

Exposes a subset of the Philips Hue local CLIP v2 API over gRPC, packaged to
run on NixOS.

Five operations are in scope: pairing, listing lights, reading a light,
updating a light, and the event stream. Everything else in the Hue API is
deliberately out of scope, as are mDNS discovery, multiple bridges, the remote
API, and Entertainment streaming.

See [`CONTEXT.md`](./CONTEXT.md) for the project's vocabulary and
[`docs/adr/`](./docs/adr) for the decisions that are hard to reverse.

## Development

```sh
nix develop          # dev shell: pytest, ruff, mypy, protoc
nix flake check      # tests, lint, typecheck, package build
nix build            # ./result/bin/hue-grpc-server
```

Run `pytest` from inside `nix develop`: the dev shell puts `src/` on
`PYTHONPATH`, which `pyproject.toml` deliberately does not do, so that the Nix
check phase exercises the installed package instead of the source tree.

Unit tests are pure Python and run on macOS. The package targets
`x86_64-linux`; NixOS VM tests run in CI, since no fast native x86_64-linux
builder is available locally.

`tests/smoke` talks to a real bridge and is excluded from the packaged test
run. Point it at one by hand:

```sh
HUE_BRIDGE_ADDRESS=192.168.86.223 HUE_BRIDGE_ID=ECB5FAFFFE334703 pytest tests/smoke
```

Pairing mints a real Application Key, so it is gated behind a second variable.
Press the bridge's link button, then run within thirty seconds:

```sh
HUE_BRIDGE_ADDRESS=... HUE_BRIDGE_ID=... HUE_PRESS_LINK_BUTTON=1 \
    pytest tests/smoke -k mints
```

That leaves an entry named `hue-grpc#smoke-test` on the bridge; nothing stores
the secrets yet, so remove it from the Hue app afterwards.

## State

The Gateway's Registry Entry for its bridge — Bridge ID, address, model,
firmware, last successful contact, and the secrets Pairing minted — is a
single JSON file at
`$STATE_DIRECTORY/registry.json`, mode 0600, written atomically. Outside
systemd it falls back to `$XDG_STATE_HOME/hue-grpc/registry.json`. It sits
outside the Nix store so a system rollback cannot discard it, and it is not
encrypted at rest; see
[ADR 0004](./docs/adr/0004-registry-on-disk-format.md) for why.

## Status

Scaffolding only. The gateway itself is tracked in the [open
issues](https://github.com/malamoney/hue/issues).
