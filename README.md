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

The event smoke tests need the same key and take about ten seconds, most of
which is spent proving the bridge holds a silent stream open:

```sh
HUE_BRIDGE_ADDRESS=... HUE_BRIDGE_ID=... HUE_APPLICATION_KEY=... \
    pytest tests/smoke -k events
```

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

A gRPC status is for the other case — the bridge could not be reached, would
not answer, or refused the exchange outright — and it carries what the bridge
said along with it. `INVALID_ARGUMENT` is the code; `invalid value,
dimming.brightness, 101` is the half of the answer that says which field to
fix, and it survives the trip. `UNAVAILABLE` is a bridge that is not there,
`DEADLINE_EXCEEDED` one that went quiet, `RESOURCE_EXHAUSTED` one that asked
to be left alone, `UNIMPLEMENTED` one whose firmware does not serve the path,
`FAILED_PRECONDITION` a bridge that no longer accepts the gateway's
application key — which is a different secret from the caller's Gateway
Token, and saying `UNAUTHENTICATED` would send them after the wrong one.

A read that loses its connection is asked again, up to three times, with a
jittered backoff bounded well inside the caller's deadline. A change is not,
ever: a `PUT` that failed after the bridge acted on it cannot be told apart
from one that failed before, and the gateway does not get to guess. A bridge
that answered — a 429, a 503 — is not asked again either; that is a decision
for the client, who can see the whole round trip.

## Events

```sh
grpcurl -plaintext -d '{}' 127.0.0.1:50051 hue.v1.EventService/Subscribe
```

That streams every change the bridge reports, for as long as the client
listens, out of the one connection the gateway holds open to the bridge —
however many clients are listening. `{"resource_ids": ["<id>"]}` and
`{"resource_types": ["RTYPE_LIGHT"]}` narrow it; an empty request is
everything.

Each event carries the bridge id, the bridge's own timestamp, the gateway's
receive time, the resource that moved and, for lights, the changed properties
typed as a `LightGet`. Resources the gateway does not model still arrive, with
their id and type and no typed update: knowing a grouped light changed is
worth more than silence.

The stream does not end when the bridge goes away. That is a `Gap`, which is a
message on the stream:

```json
{"bridgeId": "...", "gap": {"cause": "CAUSE_RECONNECTED"}}
```

A gap says events may have been missed and the gateway cannot tell whether
any were — the bridge purges its event buffer after several minutes without
signalling that it has, so a gap can never be disproven. One is emitted on
every reconnect, unconditionally, however brief the outage. What makes that
survivable is what follows it: the gateway re-reads every light and emits a
synthetic event for whatever differs — an add, an update or a delete — so a
gap is followed by the truth about the resources it models. Those synthetic
events carry no event id and no bridge timestamp, because the bridge never
sent them. See [ADR 0005](./docs/adr/0005-announce-every-gap-and-resync.md).

The stream itself reconnects on a schedule of its own: half a second,
doubling to thirty, jittered, for as long as the bridge stays away. No
client's deadline bounds it — a bridge unplugged overnight is still the
bridge, and a subscriber is told what it missed when the bridge comes back.

`CAUSE_SUBSCRIBER_BEHIND` is the other one, and it is about one client: each
subscriber has a bounded queue — `--event-queue-size`, 256 by default — and a
client that stops reading fills its own queue and nothing else. What it missed
is counted and reported in the position the events would have been. A slow
client never blocks the bridge reader or its neighbours.

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

## NixOS

The flake exports `nixosModules.default`. A minimal host:

```nix
{
  imports = [ hue-grpc.nixosModules.default ];

  services.hue-grpc = {
    enable = true;
    bridge = {
      address = "192.168.86.223";
      id = "ECB5FAFFFE334703";
      # A file of `key=value` lines: application-key=..., optionally client-key=...
      credentialsFile = "/run/secrets/hue-grpc";
    };
  };
}
```

`enable` builds a hardened systemd unit — `DynamicUser`, `StateDirectory`,
`ProtectSystem=strict`, and the rest — that runs the Gateway as
`hue-grpc.service`. The Listener defaults to `127.0.0.1:50051`; `port` and
`openFirewall` adjust that, and a non-loopback `listenAddress` is refused at
build time without `grpc.tls.enable` and `grpc.tokenFile`, the same rule the
server enforces on startup.

`bridge.*` describes one Bridge without pairing or discovery: the address and
id are configuration, and the Application Key comes from the Credentials File
named by `bridge.credentialsFile`, loaded through systemd `LoadCredential`.
`bridge.caFile` points certificate verification at a CA other than the
vendored Philips `root-bridge` — the identity check still runs on top, so it
swaps the trust anchor rather than weakening anything, for a Bridge behind a
certificate this Gateway was not shipped knowing about.
That path — with `grpc.tls.privateKeyFile` and `grpc.tokenFile` — is the only
way a secret reaches the service: never an `ExecStart` argument, a
Nix-rendered environment variable, or anything else that lands in the store.
It can come from `sops-nix`, `agenix`, or a root-owned file under
`/run/secrets`. When `bridge.*` is set the Registry file is not read at all;
leaving it unset falls back to a paired `registry.json` under the state
directory, written for now by running `hue-grpc-server pair` (see
[Pairing](#pairing)) against that same directory.

Everything the module does not surface — `--reflection`, `--log-level`, the
event queue size — is reachable through `extraArgs`.

The aggressive half of the sandbox — a syscall filter, tighter namespace and
capability limits — is a later pass, tested against a booted VM.

`checks.integration-vm` (Linux only) is that booted VM: two nodes, one running
the module's unit and one running a fake Bridge, exercising a read, a
mutation, an event stream, a restart, and a bridge interruption end to end,
and confirming the Application Key never reaches the journal.

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

Lights can be listed, read and changed over gRPC, and every change the bridge
reports can be streamed as it happens, on a gateway that pairs itself with the
bridge and remembers it across restarts. Every failure those RPCs can reach
has a status and a retry rule, and every gap in the event stream is announced
and then narrowed by a resync. The NixOS unit is tracked in the [open
issues](https://github.com/malamoney/hue/issues).
