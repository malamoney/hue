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

### Events

**Gap**:
An interval during which events may have been missed, where the Gateway cannot determine whether any were. The Bridge discards buffered events after several minutes without signalling that it has done so, so a Gap can never be disproven — only narrowed by Resync.
_Avoid_: drop, loss, missed events

**Resync**:
Re-reading full Resource state after an event stream reconnect and emitting synthetic update events for anything that changed. Converts a Gap into known state for the Resources the Gateway models.
_Avoid_: refresh, backfill, catch-up
