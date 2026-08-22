---
title: "TLS fails from every service, but the site loads fine in a browser"
date: 2026-08-22
tags:
  - target/nginx
  - layer/auth
  - symptom/outage
status: solved
severity: P1
env: "nginx 1.24 terminating TLS / 90-day public CA certificate / automated renewal at 03:00 / Go and Java clients on Alpine"
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

## Prevention

- **Detection:** probe from a client that cannot rescue itself. The existing uptime check ran from a host whose store had the intermediate and stayed green throughout. The replacement is a plain `curl` in a minimal container, plus an alert on served **chain length < 2** — which catches this exact class before a single request fails, since it is checkable at deploy time rather than at renewal time.
- **Prevention:** the deploy script asserts chain length before reloading, and refuses to install a certificate file containing only one certificate. Renewal is also staged to a canary instance first, which turns a scheduled 03:00 event into an observable one.
- **Remaining debt:** the internal PKI services deploy through the same script pattern and were not touched. They terminate mTLS, where a broken chain fails in both directions and more confusingly, and nobody has checked whether they point at `cert.pem` or `fullchain.pem`. This is known and not scheduled.

## Open questions

- Did the browsers succeed by fetching AIA at request time, or because the intermediate was already cached from earlier visits? Never distinguished, and it matters: if it is caching, then a genuinely fresh machine would have failed too, and "it works in a browser" is even less informative than assumed.
- Why the monitoring probe passed is attributed to the intermediate being present in that host's store — but whether it was installed deliberately, or is a leftover from some earlier debugging, was never established. Nobody knows how many other checks are green for the same reason.
- Whether the renewal ever produced a correct `fullchain.pem` that the deploy step then ignored, or whether the ACME step also changed, is unknown. The 03:00 run's logs had rotated by the time anyone thought to read them, which is its own small finding and is not fixed.

## Related

Parent concept: [[concepts/certificate-chains|certificate chains and trust stores]]. Filed under Auth in the [[maps/incidents-moc|incident map]].

Sibling: [[problems/df-says-the-disk-is-full-du-says-it-is-half-empty|the disk that filled with nothing on it]]. Both are changes that were reviewed and tested by a check structurally incapable of seeing the failure — `logrotate -d` reports what would be rotated and says nothing about file descriptors; the certificate deploy was verified by loading the site in a browser, which is the one client that papers over a missing intermediate. In both, the test passing was evidence of nothing at all.

Worth contrasting with [[problems/prepared-statement-errors-appear-only-under-concurrency|the prepared-statement failures under transaction pooling]]. Both were reported as "it works for me", but there the variable was load and here it is the client's trust store — so one is reproduced by turning up concurrency and the other only by changing who is asking. Reaching for a load generator here would have wasted the night.
