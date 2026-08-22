---
title: "Certificate chains and trust stores"
description: "Who is required to send which certificate, who is allowed to go looking for a missing one, and why a working browser proves nothing."
tags:
  - meta
---

A TLS server proves its identity with a **chain**: the leaf certificate for its hostname, signed by one or more intermediates, ending at a root the client already trusts.

The division of labour is the part that gets misremembered:

| Certificate | Who holds it | Who sends it |
| --- | --- | --- |
| Root CA | in the client's trust store | nobody — sending it is pointless |
| Intermediate(s) | nobody, by default | **the server, in the handshake** |
| Leaf | the server | the server |

The client is not expected to have the intermediate. That is exactly why it exists — it lets a CA sign certificates without exposing the root, and it can be rotated without touching anyone's trust store. **The server is responsible for shipping it**, which is why CAs emit a `fullchain.pem` alongside the bare `cert.pem`, and why pointing a server at the wrong one of those two files is such an easy and consequential mistake.

## Why it works for some clients anyway

A server that sends only its leaf is misconfigured, but it is not uniformly broken, because verifiers disagree about how hard to try:

- **macOS and Windows system verifiers, and browsers on them** — follow the **AIA** (Authority Information Access) extension in the leaf, fetch the missing intermediate over HTTP, and cache it. They succeed, and they keep succeeding more easily over time as the cache fills.
- **OpenSSL, Go's `crypto/x509`, Java by default** — do not chase AIA. They fail immediately with "unknown authority" or "unable to get local issuer certificate".

So the failure sorts itself by client type, not by load or by time, and the people best placed to notice — engineers on laptops, opening the site in a browser — are precisely the ones who cannot see it. This is the mechanism behind [[problems/tls-fails-from-services-but-the-site-loads-fine-in-a-browser|a site that loads perfectly in a browser while every service call to it fails]].

The check that answers it in one line — count what the server actually sends:

```bash
openssl s_client -connect example.com:443 -servername example.com -showcerts </dev/null 2>/dev/null \
  | grep -c 'BEGIN CERTIFICATE'
```

One means leaf only. Two or more means a chain is being served. A browser's green padlock does not distinguish these two cases and never will.

## Both directions, under mTLS

With mutual TLS the same obligation runs the other way as well: the **client** presents a chain, and the **server** verifies it against a configured CA bundle. Every rule above holds, mirrored — the client must send its intermediates, because the server is not expected to have them.

Two things change, and they pull in opposite directions.

**It gets easier to diagnose.** No server-side verifier chases AIA. OpenSSL does not, and nginx, HAProxy and Envoy all verify through it. There is no forgiving client to create a "works for me" split, so a leaf-only client certificate fails for everyone, immediately and identically.

**It gets harder to find.** The error is logged by the party that is *not* misconfigured. A service shipping a truncated client certificate produces nothing in its own logs beyond a generic handshake failure; the diagnosis is sitting in the server's error log, on a different host, owned by a different team:

```text
client SSL certificate verify error: (21:unable to verify the first certificate)
client SSL certificate verify error: (20:unable to get local issuer certificate)
client SSL certificate verify error: (22:certificate chain too long)
```

The first two are the missing-intermediate case. The third is a different trap: nginx defaults to `ssl_verify_depth 1`, which permits a single intermediate. A two-level issuing hierarchy fails there even when the client sends a complete and correct chain.

There is also a way to be broken and not know it. The CA bundle a server uses to verify clients frequently contains root **and** intermediates, because that is how the PKI hands it over. A client sending only its leaf then verifies perfectly — not because it is correct, but because the server happened to already hold the certificate the client should have sent. Trimming that bundle to just the root, which reads like tidying, breaks every such client at once and looks like the trim caused it.

```bash
# what a client is configured to present — the file, since you cannot s_client yourself
grep -c 'BEGIN CERTIFICATE' /etc/ssl/svc/client-fullchain.pem     # expect >= 2

# does the chain verify against the root ALONE, with no help from the server's bundle?
openssl verify -CAfile ca-root.pem -untrusted client-intermediate.pem client-leaf.pem

# end to end, from a host with nothing cached
curl --cert /etc/ssl/svc/client-fullchain.pem --key /etc/ssl/svc/client.key \
     https://api.example.internal/health
```

The middle command is the one worth internalising: verifying against the root **alone** is the only check that distinguishes a correct client from one being carried by the server's bundle.
