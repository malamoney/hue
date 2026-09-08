# Hue gRPC Gateway

A service that exposes a small subset of the Philips Hue local CLIP v2 API over gRPC, packaged to run on NixOS.

## Language

### The service and the device

**Gateway**:
This service. Translates gRPC calls into Hue CLIP v2 HTTP calls and back.
_Avoid_: proxy, server, wrapper, adapter

**Bridge**:
The physical Philips Hue Bridge device on the local network.
_Avoid_: hub, gateway, controller

**Bridge Resource**:
The CLIP resource at `/clip/v2/resource/bridge` — the Bridge's description of itself. Distinct from the Gateway's own record of that Bridge.
_Avoid_: bridge (unqualified) when the CLIP resource is meant

**Registry Entry**:
The Gateway's stored record of one Bridge: its Bridge ID, address, model, firmware, and secrets.
_Avoid_: bridge config, bridge record, bridge state

**Bridge ID**:
The Bridge's permanent identity, e.g. `ECB5FAFFFE334703`. Survives address changes and factory resets. The Registry keys on it, and it is the name asserted in the Bridge's TLS certificate.
_Avoid_: bridge name, MAC address, IP address

### Secrets

Three distinct secrets exist. "Credential" and "key" are ambiguous on their own and should not be used unqualified.

**Application Key**:
The per-application secret the Bridge issues during Pairing, sent on every CLIP v2 request as the `hue-application-key` header. The v1 API calls this field `username`; it is not a username.
_Avoid_: username, whitelist entry, token, credential, API key

**Client Key**:
A second secret issued alongside the Application Key when Pairing requests it. It is the pre-shared key for Entertainment streaming and is not used by CLIP v2.
_Avoid_: key (unqualified), clientkey, secret

**Gateway Token**:
The bearer token a gRPC client presents to the Gateway. Unrelated to the Bridge; the Bridge never sees it.
_Avoid_: API key, credential, auth key

**Credentials File**:
A file of `key=value` lines (`application-key`, optionally `client-key`) that hands the Gateway its Bridge secrets when it is configured statically rather than paired. Loaded by systemd `LoadCredential`, referenced by runtime path only, never in the Nix store. Distinct from the Registry Entry, which is where Pairing writes the same secrets.
_Avoid_: credentials (unqualified), secrets file, key file

### Lifecycle

**Pairing**:
The one-time exchange with the Bridge that mints an Application Key, requiring a physical button press. Fails with Hue error type 101 until the button is pressed.
_Avoid_: registration, authentication, linking, onboarding

**Registration**:
Persisting a paired Bridge into the Registry. Pairing and Registration fail independently and recover differently.
_Avoid_: pairing, enrollment, provisioning

### Resources

**Resource**:
An addressable object in the CLIP v2 API, identified by a UUID and a type.
_Avoid_: entity, object, item

**Device**:
A physical Zigbee product. A Device exposes one or more services.
_Avoid_: bulb, lamp, light

**Light**:
A service exposed by a Device that emits light. One Device may expose several services, only one of which is a Light.
_Avoid_: bulb, lamp, device

**Command**:
The writable half of a Resource — `LightPut` — which is a different shape from what reading one returns. A field left unset in a Command is not part of it and does not reach the Bridge, so "leave the brightness alone" and "set the brightness to zero" are different Commands.
_Avoid_: update, patch, state, payload

**Mutation**:
One Command applied to one Resource, and what the Bridge made of it. A Mutation can both succeed and fail: the Bridge reports what it changed and what it refused in the same successful exchange, and both halves are the answer.
_Avoid_: write, transaction, mutation response

### Failures

**Error Envelope**:
The `errors` array the Bridge answers with, in CLIP v2 alongside `data` and in
the v1 API one per entry. Present on failed exchanges and on successful ones
alike, so its presence says nothing about whether the request worked.
_Avoid_: error response, error body, error payload

**Safe Read**:
A request that changes nothing, and so can be sent twice without the Gateway
deciding anything. The opposite of a Mutation, which is sent once whatever
happens to it.
_Avoid_: idempotent request, retryable request, GET

### Serving

**Listener**:
The address, port and TLS settings the Gateway accepts gRPC calls on. Loopback
with TLS off by default; anything else needs both TLS and a Gateway Token.
_Avoid_: endpoint, socket, interface

**Correlation ID**:
The identifier tying every log line produced while serving one RPC together,
across the layers that produce them. Taken from the client's
`x-correlation-id` metadata when it sets one, minted otherwise.
_Avoid_: request ID, trace ID, span ID

### Events

**Gap**:
An interval during which events may have been missed, where the Gateway cannot determine whether any were. The Bridge discards buffered events after several minutes without signalling that it has done so, so a Gap can never be disproven — only narrowed by Resync.
_Avoid_: drop, loss, missed events

**Subscriber**:
One gRPC client's `Subscribe` call and the bounded queue the Gateway holds for it. Subscribers share one connection to the Bridge and are told about a Gap independently: one that falls behind loses events the others still receive.
_Avoid_: listener (which is the address the Gateway serves on), consumer, watcher, client (unqualified)

**Resync**:
Re-reading full Resource state after an event stream reconnect and emitting a synthetic event — an add, an update or a delete — for whatever differs from what the Gateway believed. Converts a Gap into known state for the Resources the Gateway models.
_Avoid_: refresh, backfill, catch-up
