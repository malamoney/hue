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

Entering the dev shell also compiles `proto/hue/v1/*.proto` into `src/hue/`,
and so does the package build. That directory is gitignored: the Python is
made from the definitions every time rather than committed alongside them, so
the two cannot drift. Regenerate it by hand with
`./tools/generate-python-protos.sh`.

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

The lighting smoke tests need an Application Key — from the environment, or
from the registry `pair` wrote:

```sh
HUE_BRIDGE_ADDRESS=... HUE_BRIDGE_ID=... HUE_APPLICATION_KEY=... \
    pytest tests/smoke -k lights
```

One of them writes to a real light, and is gated again: `HUE_CHANGE_LIGHTS=1`.
It sets a light's brightness to the brightness it already has, so nothing
should visibly happen.

## Pairing

The bridge mints an Application Key only for someone standing next to it.
Press the link button, then within thirty seconds:

```sh
hue-grpc-server pair \
    --bridge-address 192.168.86.223 \
    --bridge-id ECB5FAFFFE334703
```

That writes the registry entry the server reads on its next start. Pairing
again while an entry exists is refused: it would mint a second key and leave
the first in the bridge's app list, where only a person with the Hue app can
remove it. Following a bridge to a new address needs no new key at all.

Until an entry exists the gateway still starts, still answers health and
reflection, and answers every lighting call with `FAILED_PRECONDITION` saying
to run the above.

## Running

```sh
nix run .                                              # 127.0.0.1:50051
grpcurl -plaintext 127.0.0.1:50051 list
grpcurl -plaintext -d '{}' 127.0.0.1:50051 grpc.health.v1.Health/Check
```

The lights:

```sh
grpcurl -plaintext -d '{}' 127.0.0.1:50051 hue.v1.LightingService/ListLights
grpcurl -plaintext -d '{"light_id": "<id>"}' \
    127.0.0.1:50051 hue.v1.LightingService/GetLight
grpcurl -plaintext -d '{"light_id": "<id>", "command": {"on": {"on": false}}}' \
    127.0.0.1:50051 hue.v1.LightingService/UpdateLight
```

A field left out of `command` is left out of the request the bridge receives,
so the above turns a light off and says nothing about its brightness. That is
the whole contract: `{"dimming": {"brightness": 0}}` dims a light to nothing
and leaves it on, and a command that sets nothing at all is refused rather
than sent. Values outside the ranges Hue documents — brightness beyond 100, a
mirek below 153 — are refused too, before anything is sent, because a
mutation that half happened cannot be undone by asking again.

`UpdateLight` answers with what the bridge changed and what it refused. Both
can arrive together: a bridge that made one change and could not reach the
light for another reports both in one successful call, so Hue's own errors
travel in the response rather than as a gRPC status.

The listener defaults to loopback, TLS off, no Gateway Token: the only
configuration that is safe without anyone deciding anything, and where the
gRPC client ends up running is still undecided. Moving the listener onto the
LAN is three flags and no code:

```sh
hue-grpc-server \
    --listen-address 192.168.86.10 \
    --tls-certificate-file /run/credentials/hue-grpc.service/tls.pem \
    --tls-private-key-file /run/credentials/hue-grpc.service/tls.key \
    --gateway-token-file /run/credentials/hue-grpc.service/gateway-token
```

A listener beyond loopback is refused without both TLS and a token, and there
is no override — a TLS-terminating proxy on the same host talks to the
loopback listener. The Gateway Token comes from a file rather than a flag
because `ps` shows every argument to every user on the host.

Reflection follows the listener: on for loopback, off for anything else,
unless `--reflection on|off` says otherwise.

Logs go to stderr, one JSON object per line, carrying a correlation ID, the
method, the gRPC status, the upstream HTTP status and the duration.
`--log-format text` is the same fields for a person. Neither the Application
Key nor the Gateway Token is ever among them. A client can set its own
correlation ID with the `x-correlation-id` metadata key.

`SIGTERM` starts a graceful shutdown: health reports `NOT_SERVING` first, the
listener keeps accepting for `--shutdown-drain` seconds so a client asking can
be told, then it stops accepting, and calls still in flight have
`--shutdown-grace` seconds before they are cancelled.

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

Lights can be listed, read and changed over gRPC, on a gateway that pairs
itself with the bridge and remembers it across restarts. The event stream and
the NixOS unit are tracked in the [open
issues](https://github.com/malamoney/hue/issues), as is the rest of the error
model: the mapping from Hue's failures to gRPC status is here, but only the
part these three RPCs reach.
