# Verify the Bridge certificate against Philips' root CA with hostname checking disabled

The Bridge presents a certificate whose only identity is `CN=ecb5fafffe334703` — its Bridge ID — issued by `CN=root-bridge`, with **no** `subjectAltName` extension. Python's `ssl` module removed CN fallback years ago, so standard verification fails even with the correct CA loaded, and it would fail anyway because we connect by IP. We therefore load Philips' `root-bridge` CA as a trust anchor with `verify_mode=CERT_REQUIRED`, set `check_hostname=False`, and then **explicitly assert that the peer certificate's CN equals the expected Bridge ID**, failing the connection if it does not.

The manual CN check is not optional and is the reason this is safe. `check_hostname=False` on its own would accept any Philips-signed Bridge certificate, including a different Bridge on the same network.

## Consequences

- `check_hostname = False` appears in the transport and will read as a security bug to anyone encountering it cold. It is load-bearing, and the CN assertion immediately following it is what replaces the check being disabled. Neither line may be removed without the other.
- Philips' root CA is vendored into the repository. The Bridge serves only its leaf certificate — `unable to get local issuer certificate` — so the trust anchor never arrives over the wire and cannot be discovered at runtime.
- The expected Bridge ID is recorded in the Registry Entry at Pairing time and checked on every subsequent connection. This is what makes "identify Bridges by their stable identity, not their IP address" true rather than aspirational: the Gateway can follow a Bridge to a new address because it can prove the Bridge's identity once it arrives.
- Verification must be in place before the first `POST /api`. Pairing is the moment the Bridge mints a new Application Key and puts it on the wire; deferring verification past that point defeats it.
