# Architecture

## Goals

1. Capture the **same** DNS probe results from **multiple vantage points** simultaneously.
2. Ship every result into a single store so they can be correlated by time and dimension.
3. Make it cheap to add a new vantage point: same container image, same env vars, different label.
4. Keep the probe itself dependency-light enough to also run as a plain Python script for ad-hoc work.

## Design decisions

### Probe: single binary, multi-resolver, multi-name

`dns_probe.py` runs a tight per-interval loop that fans out across the cartesian product of:

* a list of names (`--names`)
* a list of rdtypes (`--rtypes`, default `A AAAA`)
* a list of resolvers (`--extra-resolver` plus dynamically discovered authoritative NS records for each name)

Each query produces a `ProbeResult` record (rcode, latency, all flags, answers as a list,
plus an `error` field for exceptions/timeouts) tagged with a `vantage` label baked in from
either the `--vantage` CLI flag or the `VANTAGE` env var.

UDP queries with the `TC` (truncated) bit set are automatically retried over TCP, mirroring
how real resolvers behave — otherwise we'd over-report failures for large responses.

### Telemetry: Log Ingestion API, not AMA

The Azure Monitor Agent (AMA) is designed to collect from the host OS — perf counters,
syslog, file tail, etc. It is **not** the right pattern for a custom-code container that
already has structured records in memory. The right pattern there is the **Log Ingestion
API**, where the container itself ships records directly to a Data Collection Endpoint
which routes through a Data Collection Rule into a custom table in Log Analytics.

Benefits:

* Same code path everywhere — AKS, VM, laptop. No sidecar, no DaemonSet, no host file dance.
* Native support for `DefaultAzureCredential` — Workload Identity on AKS, MI on a VM,
  `az login` on a laptop, all without code changes.
* Schema is enforced at the DCR, so consumers (KQL, Workbook) can rely on it.

The `LawShipper` class in `dns_probe.py` runs on a background thread with a bounded queue
(50k records), batches up to 500 records or ~900KB per call, and uses exponential backoff
on failure. The probe loop never blocks on shipping.

### Identity: user-assigned managed identity + Workload Identity

The Bicep creates a UAMI and grants it `Monitoring Metrics Publisher` on the DCR. This is
the only identity that needs to exist long-term — every vantage point reuses it:

* **AKS pod** — federated credential ties the UAMI to a Kubernetes ServiceAccount; the pod
  gets a projected token, `DefaultAzureCredential` exchanges it for an Azure token.
* **Azure VM** — assign the UAMI to the VM; `DefaultAzureCredential` picks it up via IMDS.
* **Laptop** — operator gets their own user assigned the same role (one-off); the cached
  `az login` token from `~/.azure` flows through.

### Storage: custom table with strongly-typed columns

`DnsProbe_CL` is defined explicitly (not auto-created from ingestion) so column types match
the probe schema. `answers` is `dynamic` (array of strings), `latency_ms` is `real`,
flags are `bool`. `TimeGenerated` is populated from the probe's `ts` field.

The DCR uses `transformKql: 'source'` because source columns map 1:1 to destination columns.
Any future enrichment (geo lookup, tagging) would go here.

## Non-goals (today)

* **Synthetic transaction beyond DNS** — connecting to the resolved IPs and exercising the
  database protocol is out of scope. If we want to confirm that DNS failures correlate with
  application failures, that needs a separate probe.
* **Per-question packet capture** — for that level of detail we'd run `tcpdump` /
  `dnsdist`-style tooling on the resolver itself.

## Future work

* **Phase 2 frontend** — a Static Web App + Azure Function that renders resolution chains
  as a directed graph (probe → recursor → authoritative), with an explainer rules engine
  that translates rcode/flag/latency combinations into plain English ("CoreDNS got SERVFAIL
  from upstream; upstream returned NXDOMAIN; cause: parent NS desync") so non-DNS-experts
  can read the data without needing to know what AD or AA means.
* **Azure VM vantage point** — add a small Bicep that spins up a `Standard_D2s_v5` with the
  UAMI attached and a systemd unit running the same container image.
* **DNSSEC validator path** — add a probe mode that walks the chain of trust explicitly and
  records validation state per RRset, since DNSSEC-mid-rollover is one of the leading
  candidates for "fails on cloud, works on laptop" patterns.
