---
title: "A 40-minute outage passed with no alert, and the error-rate panel showed a flat line"
date: 2026-08-22
tags:
  - target/prometheus
  - layer/observability
  - symptom/silent-failure
status: solved
severity: P1
env: "Prometheus 2.53 / Alertmanager 0.27 / ~30 Go services migrated to OpenTelemetry metric names"
symptom: "count by (__name__) ({__name__=~\"http_.*request.*\"}) => http_server_request_duration_seconds_count only"
root_cause: "A shared metrics module migration renamed http_requests_total six weeks earlier. Every alert built on the old name has since evaluated to an empty vector, which is not a failure state — the rules were never firing and never could."
---

*Example note — the format applied to a well-documented failure mode, not a specific engagement.*

## Symptom

- **Since when:** the outage itself began at 14:02 and was reported by a customer at 14:41. The alerting gap is older — the last time any of these rules produced a sample was six weeks earlier.
- **Reproduces when:** always. It is not a race or a load condition; the rules have been permanently inert since the rename.
- **Does not reproduce when:** nothing. There is no configuration under which they would have fired, which is what makes this worse than a flapping or throttled alert.

Alertmanager had received nothing. Prometheus showed the rule in a normal, healthy, non-firing state. And the reason:

```text
$ curl -s http://prometheus:9090/api/v1/query \
    --data-urlencode 'query=count by (__name__) ({__name__=~"http_.*request.*"})'
{"status":"success","data":{"resultType":"vector","result":[
  {"metric":{"__name__":"http_server_request_duration_seconds_count"},"value":[1755878400,"1841"]}
]}}
```

One series. `http_requests_total` — the name every alert and half the dashboards were written against — has not existed for six weeks. The rule that should have paged:

```promql
rate(http_requests_total{status=~"5.."}[5m]) > 0.05
```

returns the empty vector, and an alerting rule with an empty result has no alerts to raise. In every UI it looks exactly like a rule that is working and finding nothing wrong. The dashboard panel was not green in the sense of healthy — it was blank, and blank reads as calm.

## Environment

| | |
| --- | --- |
| Product / version | Prometheus 2.53, Alertmanager 0.27, Grafana 11 |
| Deployment | ~30 Go services, alert rules in one repo, no rule tests |
| Scale | ~180 alerting rules, ~40 of which are considered critical |
| **Last change** | **shared metrics module migrated to OpenTelemetry semantic conventions six weeks earlier, rolled out service by service. `http_requests_total` became `http_server_request_duration_seconds_count`, and the `status` label became `http_response_status_code`. Alert rules were not part of that change.** |

The rename was deliberate, reviewed, and correct. What was missing is that a metric name is an interface with consumers, and the consumers lived in a different repository that nothing connects to this one.

## Investigation

This one was investigated after the fact, which changes the character of it: the question was not "what is broken" but "why did nobody hear about it".

| Hypothesis | How it was checked | Verdict |
| --- | --- | --- |
| An Alertmanager silence swallowed the notification | `/api/v2/silences` — three active silences, none matching these labels, none created that day | rejected |
| The pager integration is broken | Test alert delivered end to end in 12 seconds | rejected |
| Prometheus was down, or missed scrapes during the window | `up` continuous across the incident; head sample rate normal; no gaps in TSDB | rejected |
| The rule fired but the threshold is too high to have caught this | Ran the rule expression against the incident window with `promtool query instant`. Not a small number — **no result at all** | accepted, and this is the turn |
| The series exists under a different name | `count by (__name__)` over the name pattern shows one series, and it is not the one the rules use | **accepted** |

Running the alert's own expression over the window of a known incident is the check that should be reflexive after any missed page, and it took an hour to get to because everyone was looking at the delivery path — silences, receivers, integrations — where failures are visible and familiar.

## Root cause

An alert compares a value against a threshold. When the series has no samples, the comparison filters an empty vector and produces an empty vector, and a rule with no results creates no alerts — see [[concepts/absent-data-is-not-zero|absent data is not zero]]. Prometheus does not consider this an error, because it is not one: an expression matching nothing is a legitimate, common, healthy state.

So the rename did not break the alerts in a way anything could report. It converted roughly forty critical rules into rules that are permanently true-of-nothing, and left every surface — rule state, Alertmanager, the panel — showing the same thing it shows when the system is fine.

The outage itself was ordinary and would have paged within two minutes under the old rules.

## Fix

Repoint the rules, and — the part that matters — pair each critical rule with a guard that fires on the *absence* of its input:

```yaml
- alert: HighErrorRate
  expr: |
    sum(rate(http_server_request_duration_seconds_count{http_response_status_code=~"5.."}[5m]))
      / sum(rate(http_server_request_duration_seconds_count[5m])) > 0.05
  for: 5m

- alert: ErrorRateSignalMissing
  expr: absent_over_time(http_server_request_duration_seconds_count[10m])
  for: 10m
  labels:
    severity: page
  annotations:
    summary: "The series HighErrorRate depends on has stopped arriving"
```

Pin the expression so a future rename fails the build rather than the next incident:

```bash
promtool test rules alerts/*_test.yml
```

And a CI check with more reach than unit tests — assert that every rule expression matches something in the real Prometheus at merge time:

```bash
promtool query instant "$PROM_URL" "$(yq '.groups[].rules[].expr' alerts/*.yml)" \
  | grep -q . || { echo "rule matches no series"; exit 1; }
```

## Prevention

- **Detection:** an absence guard for every rule tagged critical. Forty extra rules, mechanically generated from the existing ones. This is the whole fix and it is unglamorous, which is why it had never been done.
- **Prevention:** metric names are a published interface. The rename was a breaking change to consumers in another repository, and the migration checklist now includes "grep the alerting rules repo", which is a weak control that at least exists. The stronger version — recording rules as a stable indirection layer between emitters and alerts — was proposed and deferred.
- **Remaining debt:** only the rules for the service that had the outage were repointed. A sweep found **14 more rules currently matching no series**, and they have not been triaged, because "matches nothing right now" is not the same as "dead" — an alert for a rare condition legitimately matches nothing most of the time. Distinguishing them requires reading all fourteen, and nobody has.

## Open questions

- How many of the 180 rules are inert? The 14 is a lower bound taken from one instant, and there is no method proposed that separates a dead rule from a correctly quiet one without human judgement on each. Until there is, the honest answer to "are we covered" is that nobody knows.
- Did the upstream migration's release notes mention the rename? Nobody read them before the change and nobody has read them since, so it is not established whether this was documented and missed, or undocumented.
- Several Grafana panels went blank six weeks ago. Whether anyone saw a blank panel and moved on — and how many blank panels are on those dashboards right now — was not asked, and is the more uncomfortable question of the two.

## Related

Parent concept: [[concepts/absent-data-is-not-zero|absent data is not zero]]. Filed under Observability in the [[maps/incidents-moc|incident map]].

Sibling: [[problems/tls-fails-from-services-but-the-site-loads-fine-in-a-browser|the incomplete certificate chain]], whose open questions end on "nobody knows how many other checks are green for the same reason". That note's uncomfortable question is this note's root cause — there a probe passed because the host it ran from happened to hold the missing intermediate, here rules passed because the data they judge had stopped arriving. Both are monitoring reporting success about something it was not actually testing.

Contrast with [[problems/p99-latency-spikes-while-cpu-utilisation-sits-at-30-percent|the CPU throttling that hid behind healthy utilisation]]: there the metric was correct and answered the wrong question; here the metric did not exist and its absence looked identical to good news. The first is a thinking error and the second is a plumbing error, and only the second can be caught mechanically — which is an argument for spending the effort on the guards rather than on better dashboards.
