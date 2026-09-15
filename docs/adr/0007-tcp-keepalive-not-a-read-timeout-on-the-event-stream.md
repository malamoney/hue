# Find a dead Bridge under the event stream with TCP keepalive, not a read timeout

**Status:** accepted — [#38](https://github.com/malamoney/hue/issues/38).

The event stream is silent for as long as nothing in the house changes, so `hue/transport.py` gives it no read timeout: a quiet Bridge must not be mistaken for a wedged one. An SSE client also never writes. Between the two, a Bridge that disappears *without closing the connection* — it rebooted for an overnight firmware update, somebody unplugged it — leaves a socket the Gateway reads as open for as long as the process lives. `aiter_lines()` waits forever, no `HueTransportError` is raised, `fanout.py`'s `run()` never reconnects, and so the Gap and Resync that [ADR 0005](./0005-announce-every-gap-and-resync.md) promises on every reconnect never happen. Every Subscriber hears nothing, indefinitely, while reads and Mutations keep working over the transport's other connections. This is exactly what happened for 34 hours in September 2026.

The Gateway now turns on TCP keepalive on every connection the transport opens, on its own short schedule (`Keepalive`: idle 60 s, then a probe every 15 s, give up after 4). A half-open socket fails out of the read within about two minutes as an `httpx.TransportError`, which the transport already translates to `BridgeUnreachableError`, which `run()` already answers with a reconnect. Nothing above the transport changed.

## Considered Options

- **A bounded `stream_read`, treating expiry as "reconnect".** It would find the dead socket too. Rejected because [ADR 0005](./0005-announce-every-gap-and-resync.md) makes every reconnect an unconditional Gap plus a full read of the light collection: a quiet house would be told it missed events, and the Bridge re-read, every few minutes, forever, for nothing. Keepalive fires only when the peer is actually gone.
- **An application-level probe — a periodic `GET` of the light collection, diffed against the snapshot.** Rejected: it detects the symptom (state drifted) rather than the cause (the stream is dead), costs a Bridge read per tick whether or not anything is wrong, and would deliver changes late and out of order with the stream when the stream is fine.
- **Leave it to the kernel's own keepalive.** It was never on: Python sockets do not set `SO_KEEPALIVE`, and even enabled, Linux's default first probe is two hours out. Two hours of silent loss on a Gateway whose one job is to relay changes is not an acceptable detection time.

## Consequences

- The schedule lives in one place, `Keepalive`, and is applied by the transport to every Bridge connection, not only the stream; the request pool gains it for free and loses nothing.
- Detection takes roughly `idle + interval × count` — two minutes on the defaults, inside the few minutes the Bridge buffers events, so the Resync that follows is against a Bridge that has come back rather than one still rebooting.
- A dead stream is now logged as `event stream lost`, followed by a Gap and a Resync, like any other reconnect. Clients that already handle Gaps as routine ([ADR 0005](./0005-announce-every-gap-and-resync.md)) need no change.
- `TCP_KEEPIDLE` is spelled `TCP_KEEPALIVE` on macOS; the module resolves the name once so unit tests run there while the package targets Linux.
- The unit test asserts the options on the socket the stream actually rides on, not that a half-open peer is detected: a genuinely half-open TCP connection needs packets dropped on the wire, which a unit test cannot arrange. The detection itself is the kernel's contract.
