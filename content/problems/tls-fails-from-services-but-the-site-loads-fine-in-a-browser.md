---
title: "TLS fails from every service, but the site loads fine in a browser"
date: 2026-08-22
tags:
  - target/nginx
  - layer/auth
  - symptom/outage
status: solved
severity: P1
env: "nginx 1.24 terminating TLS / 90-day public CA certificate / automated renewal at 03:00 / Go and Java clients on Alpine / follow-up covers 11 internal mTLS services"
symptom: "x509: certificate signed by unknown authority"
root_cause: "The renewal deployed the leaf certificate without the intermediate, so nginx served a one-certificate chain. Clients that chase AIA fetched the missing intermediate and succeeded; OpenSSL, Go and Java did not and failed."
---

*Example note — the format applied to a well-documented failure mode, not a specific engagement.*

## Symptom

- **Since when:** 03:14 on the morning of the certificate renewal. The renewal itself reported success, and the certificate was valid, correctly dated, and for the right hostname.
- **Reproduces when:** connecting from anything using OpenSSL, Go's `crypto/x509`, or a default-configured JVM — which is every service-to-service call and every CI job.
- **Does not reproduce when:** opening the site in a browser on a Mac or Windows laptop. It loads instantly, padlock and all, from every machine anyone on the call tried.

From the Go services:

```text
Get "https://api.example.com/v1/health": tls: failed to verify certificate:
x509: certificate signed by unknown authority
```

From a CI container:

```text
curl: (60) SSL certificate problem: unable to get local issuer certificate
```

The disagreement between those two lines and a working browser is the entire diagnosis, and it was treated as noise for the first twenty minutes because "the site is up, I'm looking at it".

## Environment

| | |
| --- | --- |
| Product / version | nginx 1.24, public CA, 90-day certificate, ACME client renewing at 03:00 |
| Deployment | two nginx instances behind a network load balancer |
| Scale | ~2k internal requests/s across ~25 services, plus external browser traffic |
| **Last change** | **certificate deployment script rewritten six weeks earlier during a config cleanup. This was the first renewal to run through it.** |

A change six weeks old, detonated by a scheduled event. The renewal is not the change — the renewal is the trigger that finally executed it, and looking only at "what did we deploy today" found nothing all night.

## Investigation

| Hypothesis | How it was checked | Verdict |
| --- | --- | --- |
| The certificate is expired or for the wrong name | `openssl x509 -noout -dates -subject` on the served leaf: valid, correct SAN, issued 11 minutes before the failures began | rejected |
| Clock skew on the client hosts making a valid certificate look not-yet-valid | `chronyc tracking` on three failing hosts: offset under 2 ms | rejected |
| The container base image shipped a stale or empty CA bundle | `ca-certificates` package version identical to hosts that had been working an hour earlier. Nothing about the client changed at 03:14 | rejected — the client had not changed, which is what pointed back at the server |
| One of the two nginx instances is serving something different | Both served the same thing. Not the cause, but worth knowing before chasing an intermittent | rejected |
| The server is not sending the intermediate | `openssl s_client -showcerts` returns exactly **one** certificate | **accepted** |

```text
$ openssl s_client -connect api.example.com:443 -servername api.example.com -showcerts </dev/null 2>/dev/null \
    | grep -c 'BEGIN CERTIFICATE'
1
```

One certificate. The server was handing clients a leaf and expecting them to already know its issuer — see [[concepts/certificate-chains|certificate chains and trust stores]] for why that is the server's job and not the client's.

## Root cause

The rewritten deployment script copied `cert.pem` where the previous one had copied `fullchain.pem`. Both files exist, both are valid PEM, both contain a certificate for the right hostname, and nginx starts happily with either — the difference is that one of them also contains the intermediate.

The same file mix-up is available in the other direction. Under mTLS the client presents a chain too, and a service configured with a leaf-only client certificate fails in exactly the same way — except that the error is logged by the server it is calling, not by the service that is misconfigured. That variant is covered in the follow-up below; it is the same root cause with the roles reversed.

Clients then sorted themselves by how hard their verifier is willing to work. Browsers on macOS and Windows follow the AIA extension, fetched the intermediate over HTTP, and succeeded. OpenSSL, Go and Java do not chase AIA and failed immediately. Nothing was intermittent and nothing depended on load; the failure was perfectly deterministic per client, which is precisely why the two groups of people on the call could not agree on whether there was an incident.

## Fix

Immediate:

```bash
# nginx wants the leaf AND the intermediates, in that order, in one file
ssl_certificate     /etc/ssl/api.example.com/fullchain.pem;
ssl_certificate_key /etc/ssl/api.example.com/privkey.pem;
```

```bash
nginx -t && nginx -s reload
```

Confirm from outside, counting what is actually on the wire rather than what is on disk:

```bash
openssl s_client -connect api.example.com:443 -servername api.example.com -showcerts </dev/null 2>/dev/null \
  | grep -c 'BEGIN CERTIFICATE'      # expect >= 2
```

And a verification that does not depend on the local trust store having been contaminated by earlier successful fetches:

```bash
openssl verify -CAfile /etc/ssl/certs/ca-certificates.crt -untrusted intermediate.pem leaf.pem
```

Recovery was a reload, roughly four minutes after the chain length was checked. The preceding hour was spent looking at clients.

### The same fix on the mTLS side

For the internal services that present client certificates, the file to point at is again the one with the intermediates in it, and the server needs a CA bundle deep enough to verify what arrives:

```nginx
ssl_verify_client       on;
ssl_client_certificate  /etc/ssl/internal/ca-chain.pem;   # root + issuing intermediates
ssl_verify_depth        2;                                # default is 1: one intermediate only
```

The client side is a file choice, not a protocol setting — `curl --cert`, Go's `LoadX509KeyPair`, and a JKS key entry all want leaf **and** intermediates:

```bash
grep -c 'BEGIN CERTIFICATE' /etc/ssl/svc/client-fullchain.pem   # expect >= 2
```

The check that actually matters is verifying the client chain against the **root alone**, because that is what removes any help the server's bundle might be silently providing — see [[concepts/certificate-chains|certificate chains and trust stores]]:

```bash
openssl verify -CAfile ca-root.pem -untrusted client-intermediate.pem client-leaf.pem
```

## Prevention

- **Detection:** probe from a client that cannot rescue itself. The existing uptime check ran from a host whose store had the intermediate and stayed green throughout. The replacement is a plain `curl` in a minimal container, plus an alert on served **chain length < 2** — which catches this exact class before a single request fails, since it is checkable at deploy time rather than at renewal time.
- **Prevention:** the deploy script asserts chain length before reloading, and refuses to install a certificate file containing only one certificate. The same assertion now runs for client certificates, where it is the only automated check that exists. Renewal is also staged to a canary instance first, which turns a scheduled 03:00 event into an observable one.
- **Remaining debt:** `ssl_verify_depth` is set to 2 by convention and by nobody's decision. The issuing hierarchy is currently two levels deep, so a third level — which the PKI team could introduce without telling anyone, since it is invisible to every other consumer — fails as `(22:certificate chain too long)` on the server, with the misconfiguration nowhere near the log line. There is no alert for that string and no owner for the convention.

### Follow-up: the internal mTLS services

The audit this note originally left as debt was done a week later, across eleven services presenting client certificates. Three were shipping a leaf-only client certificate, and all three were working.

They worked because the server's `ssl_client_certificate` bundle contains the root **and** the issuing intermediates — which is simply how the PKI team hands the bundle over. Those clients were verifying against a certificate the server already held, not against one they sent. The dependency is accidental and invisible: trimming that bundle to just the root, which is exactly what a hygiene ticket would ask for, breaks all three at once and makes the trim look like the cause.

All three now ship a full chain, and the bundle contents are pinned with a comment explaining what breaks if they shrink. The test that found them is the `openssl verify` against the root alone, above — the end-to-end `curl` passes for a broken client for the same reason production did.

## Open questions

- Did the browsers succeed by fetching AIA at request time, or because the intermediate was already cached from earlier visits? Never distinguished, and it matters: if it is caching, then a genuinely fresh machine would have failed too, and "it works in a browser" is even less informative than assumed.
- Why the monitoring probe passed is attributed to the intermediate being present in that host's store — but whether it was installed deliberately, or is a leftover from some earlier debugging, was never established. Nobody knows how many other checks are green for the same reason.
- Under mTLS, is a leaf-only client certificate ever *correct* because the relying party genuinely pins the intermediate on purpose? Two of the eleven services were configured that way deliberately, according to one engineer, and no document says so. They were changed to send full chains anyway, which may have removed an intentional constraint nobody can now identify.
- Whether the renewal ever produced a correct `fullchain.pem` that the deploy step then ignored, or whether the ACME step also changed, is unknown. The 03:00 run's logs had rotated by the time anyone thought to read them, which is its own small finding and is not fixed.

## Related

Parent concept: [[concepts/certificate-chains|certificate chains and trust stores]]. Filed under Auth in the [[maps/incidents-moc|incident map]].

Sibling: [[problems/df-says-the-disk-is-full-du-says-it-is-half-empty|the disk that filled with nothing on it]]. Both are changes that were reviewed and tested by a check structurally incapable of seeing the failure — `logrotate -d` reports what would be rotated and says nothing about file descriptors; the certificate deploy was verified by loading the site in a browser, which is the one client that papers over a missing intermediate. In both, the test passing was evidence of nothing at all.

Worth contrasting with [[problems/prepared-statement-errors-appear-only-under-concurrency|the prepared-statement failures under transaction pooling]]. Both were reported as "it works for me", but there the variable was load and here it is the client's trust store — so one is reproduced by turning up concurrency and the other only by changing who is asking. Reaching for a load generator here would have wasted the night.
