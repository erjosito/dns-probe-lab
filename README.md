# dns-probe-lab

> **Diagnostic toolkit for intermittent DNS resolution failures** — a Python time-series probe,
> a containerized vantage-point image, and Azure infrastructure (Log Analytics + DCR) to
> collect comparable telemetry from multiple network locations and pinpoint where in the
> resolution chain failures originate.

This project grew out of a customer escalation where a managed-database endpoint
was intermittently returning `SERVFAIL` from inside AKS but resolving fine from a
laptop. The argument *"is it the recursive resolver, or is it the authoritative
servers?"* could only be settled with side-by-side telemetry from multiple vantage
points hitting multiple resolvers over time. That's what this is.

---

## What's in the box

| Path | Purpose |
|---|---|
| `probe/dns_probe.py` | Standalone Python probe. Queries `(name × rdtype × resolver)` tuples on a schedule, captures rcode/latency/flags/answers, ships JSONL to stdout or a file. Optionally streams to Log Analytics via the Log Ingestion API. |
| `probe/Dockerfile` | Container image (`python:3.12-slim`, non-root, pinned deps). |
| `probe/requirements.txt` | Pinned Python dependencies. |
| `infra/infra-base.bicep` | Log Analytics Workspace, custom table `DnsProbe_CL`, Data Collection Endpoint, Data Collection Rule, user-assigned managed identity with `Monitoring Metrics Publisher` on the DCR. |
| `infra/infra-aks.bicep` | AKS cluster (OIDC + Workload Identity enabled) plus federated identity credential linking the UAMI to the probe's Kubernetes ServiceAccount. |
| `infra/workbook.bicep` | Azure Monitor Workbook deployment (loads the JSON from `workbook/`). |
| `workbook/dns-probe-workbook.json` | DNS Probe Diagnostics workbook content. Edit this file to iterate on visualizations; redeploy via `workbook.bicep`. |
| `k8s/dns-probe.yaml` | Kubernetes manifest: Namespace, ServiceAccount (Workload Identity), ConfigMap, Job. Has placeholders that `deploy.ps1` substitutes. |
| `k8s/deploy.ps1` | Helper that reads the Bicep deployment outputs and renders + applies the manifest. |
| `k8s/dns-probe-job.yaml` | Kubernetes Job manifest. Pulls the probe image and runs it from inside the affected cluster as one of the vantage points. |
| `docs/architecture.md` | Architecture overview and rationale (why DCR instead of AMA, why Workload Identity, etc.). |

---

## Why multiple vantage points?

Intermittent DNS failures very rarely look the same from every observer. The same lookup
can succeed from a residential ISP, fail from one cloud region, and return a stale answer
from another. Running the same probe simultaneously from:

* a **laptop** (residential / corporate egress)
* an **AKS pod** (in the suspect cluster, using its CoreDNS → Azure DNS path)
* an **Azure VM** (clean Azure baseline, no Kubernetes layer)

…and shipping every result into one Log Analytics workspace lets you `summarize` by
`vantage`, `resolver_label`, and `rcode` and answer questions like:

* "Does this fail uniformly, or only when sourced from Azure egress IPs?"
* "Does Cloudflare (1.1.1.1) and Google (8.8.8.8) return the same answer for the same name at the same instant?"
* "When CoreDNS times out, what does a direct query to the authoritative NS return?"

---

## Architecture

```
                                           ┌──────────────────────────────┐
            ┌─ AKS Job (in-cluster) ──────►│                              │
            │                              │   Log Analytics Workspace    │
  probe ────┼─ Local podman run ──────────►│        DnsProbe_CL           │──► Workbook / KQL
            │                              │                              │
            └─ Azure VM (planned) ────────►│                              │
                                           └──────────────────────────────┘
                                                       ▲
                                                       │ Log Ingestion API
                                                       │ (DefaultAzureCredential)
                                              Data Collection Rule
                                              Data Collection Endpoint
```

The probe writes the same `ProbeResult` shape (including a `vantage` tag) regardless of
where it runs. The Log Ingestion API path uses `DefaultAzureCredential`, which works
identically with Workload Identity (AKS), system-assigned MI (VM), and `az login` (laptop).

---

## Quickstart

### 1. Deploy base infra

```powershell
az group create -n rg-dns-probe-lab -l swedencentral
az deployment group create -g rg-dns-probe-lab -f infra/infra-base.bicep
```

Note the outputs (`dceLogsIngestionEndpoint`, `dcrImmutableId`, `uamiClientId`, `lawName`).

### 1b. Deploy the workbook

```powershell
az deployment group create -g rg-dns-probe-lab -f infra/workbook.bicep `
    -p lawName=<lawName-from-step-1>
```

The deployment output includes a deep link straight to the workbook in the Azure Portal.

### 2. Build & push the image

```powershell
cd probe
podman build --network=host -t docker.io/<you>/dns-probe:latest .
podman push docker.io/<you>/dns-probe:latest
```

> `--network=host` is required for podman on Windows/WSL2 because rootless build
> containers otherwise can't reach external DNS resolvers (pip fails to resolve
> `pypi.org`). Plain `docker build` on a Linux host doesn't need it.

A pre-built image is published at `docker.io/erjosito/dns-probe:latest`.

### 3. Run a laptop vantage point

```powershell
# Grant your user Monitoring Metrics Publisher on the DCR (one-time):
$dcrId = (az deployment group show -g rg-dns-probe-lab -n <deployment-name> --query properties.outputs.dcrId.value -o tsv)
az role assignment create --assignee (az ad signed-in-user show --query id -o tsv) `
    --role "Monitoring Metrics Publisher" --scope $dcrId

podman run --rm -it `
  -e VANTAGE=jomore-desktop `
  -e LAW_DCE_ENDPOINT="https://dce-...ingest.monitor.azure.com" `
  -e LAW_DCR_IMMUTABLE_ID="dcr-..." `
  -e DNS_PROBE_NAMES="db.example.com,replica.db.example.com" `
  -e DNS_PROBE_INTERVAL=30 `
  -e DNS_PROBE_DURATION=1h `
  -v "${HOME}/.azure:/home/probe/.azure:ro" `
  docker.io/erjosito/dns-probe:latest
```

### 4. Run an AKS vantage point

Deploy the AKS cluster (creates a small lab cluster with OIDC + Workload Identity
enabled and the federated identity credential linking the UAMI to the probe's
Kubernetes ServiceAccount):

```powershell
az deployment group create -g rg-dns-probe-lab -f infra/infra-aks.bicep `
    -p uamiName=<uamiName-from-step-1>
```

Get cluster credentials, then deploy the probe Job (the helper script substitutes
the placeholders from the existing Bicep outputs):

```powershell
az aks get-credentials -g rg-dns-probe-lab -n <aksName-from-step-4>
pwsh ./k8s/deploy.ps1 -Vantage aks-swedencentral -Names "db.example.com,replica.db.example.com"

# Watch:
kubectl -n dns-probe logs -f job/dns-probe
```

The probe ships every record to the same Log Analytics workspace as the laptop
vantage point, tagged with `vantage=aks-swedencentral`, so you can compare the
two side by side in the workbook.

---

## Configuration reference

Every CLI flag has an environment-variable equivalent so the same container image can be
reused across vantage points just by changing env vars. Precedence is **CLI flag > env
var > built-in default**.

### Probe behaviour

| Env var | CLI flag | Type | Default | Notes |
|---|---|---|---|---|
| `VANTAGE` | `--vantage` | string | hostname | Tag baked into every record. Set to something stable per vantage point (e.g. `aks-westeu-sandbox`). |
| `DNS_PROBE_NAMES` | `--names` | list | `db.example.com replica.db.example.com` | Comma- or whitespace-separated list of FQDNs to probe. |
| `DNS_PROBE_RTYPES` | `--rtypes` | list | `A AAAA` | Record types per name. |
| `DNS_PROBE_INTERVAL` | `--interval` | int (seconds) | `60` | Wait between probe rounds. |
| `DNS_PROBE_DURATION` | `--duration` | string | `24h` | Total runtime. Use `30m`, `24h`, `7d`, or `0` for forever. |
| `DNS_PROBE_TIMEOUT` | `--timeout` | float (seconds) | `4.0` | Per-query timeout. |
| `DNS_PROBE_LOG_FILE` | `--log-file` | path | `dns_probe.jsonl` | JSONL output. `-` for stdout. |
| `DNS_PROBE_SUMMARY_INTERVAL` | `--summary-interval` | int (seconds) | `300` | How often the rolling stdout summary is emitted. |
| `DNS_PROBE_CONCURRENCY` | `--concurrency` | int | `16` | Parallel queries per round. |
| `DNS_PROBE_DNSSEC` | `--dnssec` | bool (`1`/`true`) | `false` | Request DNSSEC (set DO bit). |
| `DNS_PROBE_NO_AUTH_DISCOVERY` | `--no-auth-discovery` | bool | `false` | Skip dynamic authoritative-NS discovery. |
| `DNS_PROBE_EXTRA_RESOLVERS` | `--extra-resolver` | list of `LABEL=IP` | `[]` | Comma-separated, e.g. `coredns=10.0.0.10,onprem=10.1.2.3`. CLI `--extra-resolver` flags **append** to whatever is in the env var. |
| `DNS_PROBE_SKIP_RESOLVERS` | `--skip-resolver` | list of labels | `[]` | Drop one or more of the built-in resolvers, e.g. `azure-dns`. CLI flags append. |
| `DNS_PROBE_NO_REACHABILITY_CHECK` | `--no-reachability-check` | bool | `false` | At startup the probe sends one benign query to each default + authoritative-NS target and drops unreachable ones (e.g. Azure DNS from outside Azure, IPv6 NS IPs from an IPv4-only host) so the workspace isn't flooded with `EXCEPTION` rows. Set this to disable the filter and probe everything. |
| `DNS_PROBE_REACHABILITY_TIMEOUT` | `--reachability-timeout` | float (seconds) | `2.0` | Per-target timeout for the reachability check. |

### Log Analytics shipping (optional)

If `LAW_DCE_ENDPOINT` and `LAW_DCR_IMMUTABLE_ID` are both set, every probe record is also
shipped to Log Analytics via the Log Ingestion API on a background thread. If either is
missing, the probe runs purely locally (JSONL only).

| Env var | CLI flag | Default | Notes |
|---|---|---|---|
| `LAW_DCE_ENDPOINT` | `--workspace-endpoint` | — | DCE URL, e.g. `https://dce-foo.swedencentral-1.ingest.monitor.azure.com`. |
| `LAW_DCR_IMMUTABLE_ID` | `--workspace-dcr-id` | — | DCR immutable ID, e.g. `dcr-abc123...`. |
| `LAW_STREAM_NAME` | `--workspace-stream` | `Custom-DnsProbe_CL` | Must match the `streamDeclarations` key in the DCR. |

> **There is intentionally no "workspace ID" parameter.** The Log Ingestion API targets a
> Data Collection Endpoint + Data Collection Rule, not the LAW directly. The DCR routes
> the data into the workspace and table.

---

## Example KQL

```kusto
// SERVFAIL rate per vantage × resolver, over the last hour, per minute
DnsProbe_CL
| where TimeGenerated > ago(1h)
| where name in ("db.example.com", "replica.db.example.com")
| summarize
    total = count(),
    servfail = countif(rcode == "SERVFAIL"),
    timeouts = countif(error != "")
    by bin(TimeGenerated, 1m), vantage, resolver_label
| extend failure_rate = (todouble(servfail) + todouble(timeouts)) / todouble(total)
| render timechart
```

---

## Status

| Component | State |
|---|---|
| `dns_probe.py` core | ✅ Working |
| Log Ingestion API shipper | ✅ Code complete (in initial validation) |
| `infra-base.bicep` | ✅ Deployed and validated |
| `infra/workbook.bicep` + workbook JSON | ✅ Deployed |
| Container image | ✅ Pushed to `docker.io/erjosito/dns-probe:latest` |
| AKS vantage point | 🚧 In progress |
| Frontend (resolution-chain viz + explainer) | ⏳ Roadmap (deferred — workbook covers V1) |

---

## License

MIT — see [`LICENSE`](./LICENSE).
