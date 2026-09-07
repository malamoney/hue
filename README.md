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

## Status

Scaffolding only. The gateway itself is tracked in the [open
issues](https://github.com/malamoney/hue/issues).
