"""The gRPC listener: the side of the Gateway that clients talk to.

`hue_grpc.hue` is the other end, and knows nothing about gRPC. Nothing here
knows how to reach a Bridge.
"""

from __future__ import annotations
