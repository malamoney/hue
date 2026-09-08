# Announce a Gap on every reconnect and resync, rather than resume the event stream

The Bridge's event stream can be resumed: `If-None-Match` takes the timestamp of the last event seen, and the Bridge replays anything newer still in its buffer. Its own documentation then removes the value of that: it "does not maintain a persistent buffer and purges old events after several minutes **and does not indicate when the value of If-None-Match falls outside of its buffered window**". A resumed stream and a complete one are byte-for-byte identical, so resuming cannot tell a client it saw everything — it can only make it look that way.

So the Gateway does not resume. Every reconnect emits a `Gap` to every subscriber, unconditionally and whatever the outage's length, and is immediately followed by a **Resync**: a full read of the light collection, diffed against what the Gateway last believed, published as ordinary synthetic events — an add, an update or a delete, whichever the difference is. The unprovable claim ("you missed nothing") is replaced by two provable ones: "events may have been missed" and "here is what is different now".

A subscriber's own queue overflow is announced the same way, with the same message type and a count, because from the client's side it is the same fact.

## Considered Options

- **Resume with `If-None-Match` and stay quiet when the replay looks contiguous.** Rejected. There is nothing to check contiguity against — event ids are not a sequence and the Bridge signals nothing when the window has passed — so "looks contiguous" would mean "the Bridge sent something", which is true of every reconnect.
- **Suppress the Gap when the outage was shorter than the buffer window.** Rejected, and it is the tempting one: a 200ms blip almost certainly lost nothing. But "several minutes" is the only published figure, the buffer is a size as well as a duration, and a Gateway that is wrong once has trained every client to believe a guarantee it does not have. An unconditional announcement is a weaker promise that is always true.
- **Resync every Resource type rather than lights.** Rejected for now. Resync is only affordable because the modelled subset is one small collection; re-reading everything a Bridge knows about, on reconnect, aims a burst at the device that just stopped answering. Events for unmodelled types are still passed on — they are simply not claimed to be known.
- **Unbounded per-subscriber queues, so nothing is ever dropped.** Rejected: it moves the failure from one slow client to the Gateway's memory, and the reader must never be the thing that waits.

## Consequences

- Clients must handle `Gap` as a normal, routine message rather than an error. A Bridge firmware update, a Wi-Fi hiccup or a Gateway restart all produce one, and a client that treats it as a fault will treat ordinary Tuesdays as faults.
- Every reconnect costs one full read of the light collection. That is the price of the guarantee and it is bounded by the reconnect backoff's ceiling, not by how often the Bridge misbehaves.
- Resync events are indistinguishable from Bridge events except that their `event_id` is empty and they carry no `bridge_time`. This is deliberate: a client applying state does not need to care, and one that does can tell.
- The Gateway holds a snapshot of every light in memory, updated by both the live stream and each Resync. It is the smallest thing that lets a Resync report only the news; without it every reconnect would republish every light.
- `If-None-Match` and `Last-Event-ID` are unused, and the stream's `id:` lines are parsed for nothing. Any future use of them is an addition to this design, not a replacement for it: narrowing a Gap is not closing one.
