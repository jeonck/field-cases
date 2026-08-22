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
