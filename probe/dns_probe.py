#!/usr/bin/env python3
"""
dns_probe.py — time-series DNS probe for diagnosing intermittent resolution failures.

For each probe round, the script queries every (name x record-type x resolver) combination
in parallel and records:
    - Return code (NOERROR / SERVFAIL / NXDOMAIN / REFUSED / TIMEOUT / EXCEPTION)
    - Latency in milliseconds
    - DNS flags (AA, AD, TC)
    - Answer RRs
    - Errors (if any)

Resolvers probed by default:
    - Azure DNS                  168.63.129.16   (only reachable inside Azure)
    - Cloudflare public          1.1.1.1, 1.0.0.1
    - Google public              8.8.8.8, 8.8.4.4
    - Quad9                      9.9.9.9
    - All authoritative NS for the queried zone (discovered dynamically)
    - Any --extra-resolver IPs you supply (e.g. an in-cluster CoreDNS, internal
      resolver, or a known sub-delegation NS for the affected sub-zone).

Output:
    - JSONL log file (one line per query, machine-readable for later analysis)
    - Rolling summary to stdout every --summary-interval seconds, showing success
      rate, p50/p95 latency, and rcode distribution per (resolver, name, type).

Typical usage:

    pip install dnspython
    python3 dns_probe.py \\
        --names db.example.com replica.db.example.com \\
        --interval 60 --duration 24h \\
        --log-file /var/log/dns_probe.jsonl

    # Add a known sub-delegation NS you want to probe directly:
    python3 dns_probe.py --extra-resolver atlas-ns1=ns-123.atlas-cloud.com

    # Compare DNSSEC-validating vs not (run two side-by-side):
    python3 dns_probe.py --log-file no-dnssec.jsonl
    python3 dns_probe.py --log-file with-dnssec.jsonl --dnssec
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import queue
import signal
import socket
import statistics
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

try:
    import dns.exception
    import dns.flags
    import dns.message
    import dns.query
    import dns.rcode
    import dns.rdatatype
    import dns.resolver
except ImportError:
    sys.stderr.write("ERROR: dnspython is not installed. Run: pip install dnspython\n")
    sys.exit(2)

# Optional dependencies: only required when --workspace-endpoint is set.
try:
    from azure.identity import DefaultAzureCredential
    from azure.monitor.ingestion import LogsIngestionClient
    from azure.core.exceptions import HttpResponseError
    _LAW_AVAILABLE = True
except ImportError:
    _LAW_AVAILABLE = False


# ---- defaults ---------------------------------------------------------------

DEFAULT_NAMES = ["db.example.com", "replica.db.example.com"]
DEFAULT_RTYPES = ["A", "AAAA", "SRV", "TXT", "NS"]

DEFAULT_RESOLVERS: dict[str, str] = {
    "azure-dns":    "168.63.129.16",
    "cloudflare-1": "1.1.1.1",
    "cloudflare-2": "1.0.0.1",
    "google-1":     "8.8.8.8",
    "google-2":     "8.8.4.4",
    "quad9":        "9.9.9.9",
}

# Resolver used to discover authoritative nameservers at startup.
DISCOVERY_RESOLVER = "1.1.1.1"


# ---- data structures --------------------------------------------------------

@dataclass
class ProbeResult:
    ts: str
    vantage: str
    resolver_label: str
    resolver_ip: str
    name: str
    rdtype: str
    rcode: str
    latency_ms: float | None
    rd_flag: bool
    do_flag: bool
    aa_flag: bool | None
    ad_flag: bool | None
    tc_flag: bool | None
    answer_count: int
    answers: list[str] = field(default_factory=list)
    error: str | None = None


# ---- Log Analytics Workspace shipper ----------------------------------------

class LawShipper:
    """
    Non-blocking background uploader that ships ProbeResult records to a Log
    Analytics Workspace via the Log Ingestion API (DCR-based).

    Configured via:
        --workspace-endpoint     https://<dce-name>.<region>-1.ingest.monitor.azure.com
        --workspace-dcr-id       Immutable ID of the DCR (e.g. dcr-abc123...)
        --workspace-stream       Custom stream name in the DCR (e.g. Custom-DnsProbe_CL)

    Authentication is via DefaultAzureCredential (works with Workload Identity
    in AKS, system-assigned MI on a VM, and `az login` on a laptop).
    """

    # Single-batch ceilings imposed by the Log Ingestion API
    _MAX_BATCH_RECORDS = 500
    _MAX_BATCH_BYTES = 900_000     # API limit ~1MB, leave headroom for JSON overhead

    def __init__(self, endpoint: str, dcr_id: str, stream_name: str,
                 *, max_queue: int = 50_000, flush_interval: float = 5.0) -> None:
        if not _LAW_AVAILABLE:
            raise RuntimeError(
                "Log Analytics shipping requested but optional deps are missing. "
                "Install: pip install azure-identity azure-monitor-ingestion"
            )
        self.endpoint = endpoint
        self.dcr_id = dcr_id
        self.stream_name = stream_name
        self.flush_interval = flush_interval
        self._q: queue.Queue[dict] = queue.Queue(maxsize=max_queue)
        self._stop = threading.Event()
        self._pending: dict | None = None       # one-record overflow slot
        self._client = LogsIngestionClient(
            endpoint=endpoint,
            credential=DefaultAzureCredential(),
            logging_enable=False,
        )
        self._thread = threading.Thread(target=self._run, name="law-shipper", daemon=True)
        self.stats = {"sent": 0, "dropped_full_queue": 0, "failed": 0, "batches": 0}

    def start(self) -> None:
        self._thread.start()

    def submit(self, record: dict) -> None:
        """Non-blocking submission. Drops on full queue rather than blocking the probe."""
        try:
            self._q.put_nowait(record)
        except queue.Full:
            self.stats["dropped_full_queue"] += 1

    def stop(self, timeout: float = 30.0) -> None:
        self._stop.set()
        self._thread.join(timeout=timeout)
        sys.stderr.write(
            f"[law] shipper stopped. stats={self.stats}\n"
        )

    def _drain(self) -> list[dict]:
        out: list[dict] = []
        running_size = 0
        while len(out) < self._MAX_BATCH_RECORDS:
            try:
                rec = self._q.get_nowait()
            except queue.Empty:
                break
            rec_size = len(json.dumps(rec, ensure_ascii=False)) + 1
            if running_size + rec_size > self._MAX_BATCH_BYTES and out:
                # Push the record back; will be picked up next batch.
                self._q.queue.appendleft(rec)
                break
            out.append(rec)
            running_size += rec_size
        return out

    def _send(self, batch: list[dict]) -> None:
        # Exponential backoff on transient failures; never crash the shipper thread.
        delay = 1.0
        for attempt in range(6):
            try:
                self._client.upload(
                    rule_id=self.dcr_id,
                    stream_name=self.stream_name,
                    logs=batch,
                )
                self.stats["sent"] += len(batch)
                self.stats["batches"] += 1
                return
            except HttpResponseError as exc:
                status = getattr(exc, "status_code", None) or 0
                if status in (408, 429, 500, 502, 503, 504) and attempt < 5:
                    sys.stderr.write(
                        f"[law] transient {status}, retry in {delay:.1f}s "
                        f"(attempt {attempt + 1}/6)\n"
                    )
                    time.sleep(delay)
                    delay = min(delay * 2, 30.0)
                    continue
                sys.stderr.write(
                    f"[law] permanent failure status={status}: {exc.message}\n"
                )
                self.stats["failed"] += len(batch)
                return
            except Exception as exc:  # noqa: BLE001
                sys.stderr.write(
                    f"[law] unexpected error {type(exc).__name__}: {exc} "
                    f"(retry in {delay:.1f}s)\n"
                )
                if attempt < 5:
                    time.sleep(delay)
                    delay = min(delay * 2, 30.0)
                    continue
                self.stats["failed"] += len(batch)
                return

    def _run(self) -> None:
        next_flush = time.monotonic() + self.flush_interval
        while not self._stop.is_set() or not self._q.empty():
            now = time.monotonic()
            # Flush when interval elapsed OR queue is near a batch boundary
            should_flush = (
                now >= next_flush
                or self._q.qsize() >= self._MAX_BATCH_RECORDS
                or self._stop.is_set()
            )
            if should_flush:
                batch = self._drain()
                if batch:
                    self._send(batch)
                next_flush = time.monotonic() + self.flush_interval
            else:
                time.sleep(0.1)


# ---- core probe -------------------------------------------------------------

def query_once(
    *,
    vantage: str,
    resolver_label: str,
    resolver_ip: str,
    qname: str,
    rdtype: str,
    want_recursion: bool,
    want_dnssec: bool,
    timeout: float,
) -> ProbeResult:
    """Issue a single DNS query against a specific resolver IP and capture details."""
    ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    q = dns.message.make_query(
        qname,
        dns.rdatatype.from_text(rdtype),
        use_edns=0,
        want_dnssec=want_dnssec,
    )
    if not want_recursion:
        q.flags &= ~dns.flags.RD

    start = time.monotonic()
    try:
        response = dns.query.udp(q, resolver_ip, timeout=timeout)
        if response.flags & dns.flags.TC:
            response = dns.query.tcp(q, resolver_ip, timeout=timeout)
        elapsed_ms = (time.monotonic() - start) * 1000

        rcode = dns.rcode.to_text(response.rcode())
        aa = bool(response.flags & dns.flags.AA)
        ad = bool(response.flags & dns.flags.AD)
        tc = bool(response.flags & dns.flags.TC)

        answers: list[str] = []
        for rrset in response.answer:
            for rr in rrset:
                answers.append(
                    f"{rrset.name.to_text()} {rrset.ttl} "
                    f"{dns.rdatatype.to_text(rrset.rdtype)} {rr.to_text()}"
                )
        return ProbeResult(
            ts=ts,
            vantage=vantage,
            resolver_label=resolver_label,
            resolver_ip=resolver_ip,
            name=qname,
            rdtype=rdtype,
            rcode=rcode,
            latency_ms=round(elapsed_ms, 2),
            rd_flag=want_recursion,
            do_flag=want_dnssec,
            aa_flag=aa,
            ad_flag=ad,
            tc_flag=tc,
            answer_count=len(answers),
            answers=answers,
        )
    except dns.exception.Timeout:
        elapsed_ms = (time.monotonic() - start) * 1000
        return ProbeResult(
            ts=ts,
            vantage=vantage,
            resolver_label=resolver_label,
            resolver_ip=resolver_ip,
            name=qname,
            rdtype=rdtype,
            rcode="TIMEOUT",
            latency_ms=round(elapsed_ms, 2),
            rd_flag=want_recursion,
            do_flag=want_dnssec,
            aa_flag=None,
            ad_flag=None,
            tc_flag=None,
            answer_count=0,
        )
    except Exception as exc:  # noqa: BLE001 - we want to record everything
        elapsed_ms = (time.monotonic() - start) * 1000
        return ProbeResult(
            ts=ts,
            vantage=vantage,
            resolver_label=resolver_label,
            resolver_ip=resolver_ip,
            name=qname,
            rdtype=rdtype,
            rcode="EXCEPTION",
            latency_ms=round(elapsed_ms, 2),
            rd_flag=want_recursion,
            do_flag=want_dnssec,
            aa_flag=None,
            ad_flag=None,
            tc_flag=None,
            answer_count=0,
            error=f"{type(exc).__name__}: {exc}",
        )


# ---- authoritative discovery ------------------------------------------------

def discover_authoritative_ns(
    name: str,
    via_resolver: str = DISCOVERY_RESOLVER,
) -> list[tuple[str, str]]:
    """
    Walk up the labels of `name` until an NS RRset is returned. For each NS,
    resolve A and AAAA. Return list of (label, ip) suitable for use as probe targets.
    Label format: 'auth:<zone>:<ns-hostname>'.
    """
    labels = name.strip(".").split(".")
    res = dns.resolver.Resolver(configure=False)
    res.nameservers = [via_resolver]
    res.timeout = 4
    res.lifetime = 6

    chosen_zone = ""
    ns_names: list[str] = []
    for i in range(len(labels)):
        candidate = ".".join(labels[i:])
        try:
            ans = res.resolve(candidate, "NS")
            ns_names = sorted({rr.target.to_text().rstrip(".") for rr in ans})
            chosen_zone = candidate
            break
        except Exception:
            continue

    out: list[tuple[str, str]] = []
    for ns in ns_names:
        for qtype in ("A", "AAAA"):
            try:
                ans = res.resolve(ns, qtype)
                for rr in ans:
                    out.append((f"auth:{chosen_zone}:{ns}", rr.to_text()))
            except Exception:
                continue
    return out


# ---- runner -----------------------------------------------------------------

class Probe:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        # counters[(resolver_label, resolver_ip, name, rdtype)][rcode] = count
        self.counters: dict[tuple[str, str, str, str], dict[str, int]] = defaultdict(
            lambda: defaultdict(int)
        )
        self.latencies: dict[tuple[str, str, str, str], list[float]] = defaultdict(list)
        self.stop = False
        self.targets: list[tuple[str, str]] = []
        self.last_summary = time.monotonic()
        self.law: LawShipper | None = None
        if args.workspace_endpoint and args.workspace_dcr_id and args.workspace_stream:
            self.law = LawShipper(
                endpoint=args.workspace_endpoint,
                dcr_id=args.workspace_dcr_id,
                stream_name=args.workspace_stream,
            )

    def install_signal_handlers(self) -> None:
        def handler(signum, _frame):
            sys.stderr.write(f"\n[signal] received {signum}, shutting down...\n")
            self.stop = True

        signal.signal(signal.SIGINT, handler)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, handler)

    def build_target_list(self) -> None:
        skip = set(self.args.skip_resolver or [])
        for label, ip in DEFAULT_RESOLVERS.items():
            if label in skip:
                continue
            self.targets.append((label, ip))

        for spec in self.args.extra_resolver or []:
            if "=" in spec:
                label, ip = spec.split("=", 1)
            else:
                label, ip = spec, spec
            self.targets.append((label.strip(), ip.strip()))

        if not self.args.no_auth_discovery:
            seen = set()
            for n in self.args.names:
                try:
                    discovered = discover_authoritative_ns(n)
                except Exception as exc:  # noqa: BLE001
                    sys.stderr.write(
                        f"[init] WARNING: authoritative discovery failed for {n}: {exc}\n"
                    )
                    discovered = []
                for label, ip in discovered:
                    key = (label, ip)
                    if key in seen:
                        continue
                    seen.add(key)
                    self.targets.append(key)

        sys.stderr.write(f"[init] vantage: {self.args.vantage}\n")
        sys.stderr.write(f"[init] {len(self.targets)} probe targets:\n")
        for label, ip in self.targets:
            sys.stderr.write(f"  - {label:<54} {ip}\n")
        sys.stderr.write(f"[init] names:  {', '.join(self.args.names)}\n")
        sys.stderr.write(f"[init] rtypes: {', '.join(self.args.rtypes)}\n")
        sys.stderr.write(
            f"[init] interval={self.args.interval}s duration={self.args.duration} "
            f"timeout={self.args.timeout}s dnssec={self.args.dnssec}\n"
        )
        sys.stderr.flush()

    def one_round(self, log_fh) -> None:
        jobs = []
        results: list[ProbeResult] = []
        with ThreadPoolExecutor(max_workers=self.args.concurrency) as pool:
            for name in self.args.names:
                for rdtype in self.args.rtypes:
                    for label, ip in self.targets:
                        # Authoritative servers: RD=0 (no recursion). Recursors: RD=1.
                        want_recursion = not label.startswith("auth:")
                        jobs.append(
                            pool.submit(
                                query_once,
                                vantage=self.args.vantage,
                                resolver_label=label,
                                resolver_ip=ip,
                                qname=name,
                                rdtype=rdtype,
                                want_recursion=want_recursion,
                                want_dnssec=self.args.dnssec,
                                timeout=self.args.timeout,
                            )
                        )
            for fut in as_completed(jobs):
                try:
                    results.append(fut.result())
                except Exception as exc:  # noqa: BLE001
                    sys.stderr.write(f"[round] probe error: {exc}\n")

        for r in results:
            record = asdict(r)
            log_fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            if self.law is not None:
                # LAW DCR expects 'TimeGenerated' as the record timestamp.
                law_record = dict(record)
                law_record["TimeGenerated"] = record["ts"]
                self.law.submit(law_record)
            key = (r.resolver_label, r.resolver_ip, r.name, r.rdtype)
            self.counters[key][r.rcode] += 1
            if r.latency_ms is not None and r.rcode == "NOERROR":
                self.latencies[key].append(r.latency_ms)
        log_fh.flush()

    def maybe_print_summary(self, force: bool = False) -> None:
        if not force and (time.monotonic() - self.last_summary) < self.args.summary_interval:
            return
        self.last_summary = time.monotonic()
        sys.stdout.write("\n" + "=" * 120 + "\n")
        sys.stdout.write(
            f"Rolling summary at {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
        )
        sys.stdout.write("=" * 120 + "\n")
        header = (
            f"{'resolver':<44} {'ip':<40} {'name':<28} {'type':<6} "
            f"{'total':>6} {'ok%':>6} {'p50ms':>7} {'p95ms':>7}  rcodes\n"
        )
        sys.stdout.write(header)
        for key in sorted(self.counters):
            label, ip, name, rdtype = key
            counts = self.counters[key]
            total = sum(counts.values())
            ok = counts.get("NOERROR", 0)
            ok_pct = (ok / total * 100) if total else 0.0
            lats = self.latencies.get(key, [])
            if lats:
                p50 = round(statistics.median(lats), 1)
                if len(lats) >= 20:
                    p95 = round(statistics.quantiles(lats, n=20)[18], 1)
                else:
                    p95 = round(max(lats), 1)
                p50_s = f"{p50:>7}"
                p95_s = f"{p95:>7}"
            else:
                p50_s = p95_s = f"{'n/a':>7}"
            rcodes_s = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
            sys.stdout.write(
                f"{label:<44} {ip:<40} {name:<28} {rdtype:<6} "
                f"{total:>6} {ok_pct:>5.1f}% {p50_s} {p95_s}  {rcodes_s}\n"
            )
        sys.stdout.flush()

    def run(self) -> None:
        self.install_signal_handlers()
        self.build_target_list()

        if self.law is not None:
            sys.stderr.write(
                f"[law] shipping to {self.args.workspace_endpoint} "
                f"DCR={self.args.workspace_dcr_id} stream={self.args.workspace_stream}\n"
            )
            self.law.start()

        with _open_log(self.args.log_file) as log_fh:
            deadline = (
                time.monotonic() + self.args.duration_seconds
                if self.args.duration_seconds > 0
                else None
            )
            round_no = 0
            while not self.stop:
                round_no += 1
                t0 = time.monotonic()
                sys.stderr.write(
                    f"[round {round_no}] start "
                    f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
                )
                sys.stderr.flush()
                try:
                    self.one_round(log_fh)
                except Exception as exc:  # noqa: BLE001
                    sys.stderr.write(f"[round {round_no}] error: {exc}\n")
                self.maybe_print_summary()
                if deadline is not None and time.monotonic() >= deadline:
                    break
                elapsed = time.monotonic() - t0
                sleep_for = max(0.0, self.args.interval - elapsed)
                end_sleep = time.monotonic() + sleep_for
                while time.monotonic() < end_sleep and not self.stop:
                    time.sleep(min(1.0, end_sleep - time.monotonic()))

            self.maybe_print_summary(force=True)
            sys.stderr.write(f"\n[done] log written to {self.args.log_file}\n")

        if self.law is not None:
            self.law.stop()


# ---- helpers ----------------------------------------------------------------

_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> int:
    """Parse '30s', '5m', '24h', '7d', '0' (forever), or a bare int (seconds)."""
    text = text.strip().lower()
    if text in ("", "0"):
        return 0
    if text[-1] in _DURATION_UNITS:
        return int(float(text[:-1]) * _DURATION_UNITS[text[-1]])
    return int(text)


@contextlib.contextmanager
def _open_log(path: str):
    if path in ("-", "/dev/stdout"):
        yield sys.stdout
        return
    p = Path(path)
    if p.parent and not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
    fh = p.open("a", encoding="utf-8")
    try:
        yield fh
    finally:
        fh.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    # ---- env-var helpers -----------------------------------------------------
    # Every CLI option below can also be set via an environment variable so the
    # same container image can run in many roles without command-line overrides.
    # CLI flags always win over env vars; env vars always win over hard-coded
    # defaults.
    def _env_str(name: str, default: str | None = None) -> str | None:
        v = os.environ.get(name)
        return v if v is not None and v != "" else default

    def _env_int(name: str, default: int) -> int:
        v = os.environ.get(name)
        if v is None or v == "":
            return default
        try:
            return int(v)
        except ValueError:
            print(f"WARN: env {name}={v!r} is not an int, ignoring", file=sys.stderr)
            return default

    def _env_float(name: str, default: float) -> float:
        v = os.environ.get(name)
        if v is None or v == "":
            return default
        try:
            return float(v)
        except ValueError:
            print(f"WARN: env {name}={v!r} is not a float, ignoring", file=sys.stderr)
            return default

    def _env_bool(name: str, default: bool = False) -> bool:
        v = os.environ.get(name)
        if v is None or v == "":
            return default
        return v.strip().lower() in ("1", "true", "yes", "y", "on")

    def _env_list(name: str, default: list[str] | None = None) -> list[str] | None:
        # Accept comma-, semicolon-, whitespace-separated values (any mix).
        # Useful for list-type options like --names, --rtypes, --extra-resolver.
        v = os.environ.get(name)
        if v is None or v == "":
            return default
        parts: list[str] = []
        for chunk in v.replace(";", ",").replace("\n", ",").split(","):
            parts.extend(s for s in chunk.split() if s)
        return parts if parts else default

    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--vantage", default=_env_str("VANTAGE", socket.gethostname()),
                   help="Vantage-point label baked into every record "
                        "(e.g. 'aks-westeu-sandbox', 'home-desktop', 'aws-us-east'). "
                        "env: VANTAGE. Default: this machine's hostname.")
    p.add_argument("--names", nargs="+",
                   default=_env_list("DNS_PROBE_NAMES", list(DEFAULT_NAMES)),
                   help="FQDNs to probe (env: DNS_PROBE_NAMES, comma/space-separated). "
                        "Default: %(default)s")
    p.add_argument("--rtypes", nargs="+",
                   default=_env_list("DNS_PROBE_RTYPES", list(DEFAULT_RTYPES)),
                   help="Record types to probe (env: DNS_PROBE_RTYPES, comma/space-separated). "
                        "Default: %(default)s")
    p.add_argument("--interval", type=int,
                   default=_env_int("DNS_PROBE_INTERVAL", 60),
                   help="Seconds between rounds (env: DNS_PROBE_INTERVAL). "
                        "Default: %(default)s")
    p.add_argument("--duration",
                   default=_env_str("DNS_PROBE_DURATION", "24h"),
                   help="Total runtime, e.g. 30m, 24h, 7d, 0=forever "
                        "(env: DNS_PROBE_DURATION). Default: %(default)s")
    p.add_argument("--timeout", type=float,
                   default=_env_float("DNS_PROBE_TIMEOUT", 4.0),
                   help="Per-query timeout in seconds (env: DNS_PROBE_TIMEOUT). "
                        "Default: %(default)s")
    p.add_argument("--log-file",
                   default=_env_str("DNS_PROBE_LOG_FILE", "dns_probe.jsonl"),
                   help="Output JSONL log path. Use '-' for stdout "
                        "(env: DNS_PROBE_LOG_FILE). Default: %(default)s")
    p.add_argument("--summary-interval", type=int,
                   default=_env_int("DNS_PROBE_SUMMARY_INTERVAL", 300),
                   help="Seconds between rolling stdout summaries "
                        "(env: DNS_PROBE_SUMMARY_INTERVAL). Default: %(default)s")
    p.add_argument("--concurrency", type=int,
                   default=_env_int("DNS_PROBE_CONCURRENCY", 16),
                   help="Parallel queries per round (env: DNS_PROBE_CONCURRENCY). "
                        "Default: %(default)s")
    p.add_argument("--dnssec", action="store_true",
                   default=_env_bool("DNS_PROBE_DNSSEC", False),
                   help="Set DO bit (request DNSSEC) on queries "
                        "(env: DNS_PROBE_DNSSEC=1)")
    p.add_argument("--no-auth-discovery", action="store_true",
                   default=_env_bool("DNS_PROBE_NO_AUTH_DISCOVERY", False),
                   help="Skip discovering and probing authoritative NS "
                        "(env: DNS_PROBE_NO_AUTH_DISCOVERY=1)")
    # For --extra-resolver / --skip-resolver, env-var entries form the *starting*
    # list and any --extra-resolver / --skip-resolver flags on the command line
    # append to that list.
    p.add_argument("--extra-resolver", action="append",
                   default=_env_list("DNS_PROBE_EXTRA_RESOLVERS", []),
                   metavar="LABEL=IP",
                   help="Additional resolver to probe (repeatable). "
                        "e.g. --extra-resolver coredns=10.0.0.10. "
                        "env: DNS_PROBE_EXTRA_RESOLVERS, comma-separated "
                        "(e.g. 'coredns=10.0.0.10,onprem=10.1.2.3').")
    p.add_argument("--skip-resolver", action="append",
                   default=_env_list("DNS_PROBE_SKIP_RESOLVERS", []),
                   metavar="LABEL",
                   help="Resolver label to skip from defaults (repeatable). "
                        "e.g. --skip-resolver azure-dns. "
                        "env: DNS_PROBE_SKIP_RESOLVERS, comma-separated.")
    # ---- Log Analytics shipping (optional) ----
    # Note: there is no "workspace ID" parameter — the Log Ingestion API targets
    # a Data Collection Endpoint + Data Collection Rule, not the LAW directly.
    # The DCR's outputStream determines which workspace + table the data lands in.
    p.add_argument("--workspace-endpoint",
                   default=_env_str("LAW_DCE_ENDPOINT"),
                   help="Data Collection Endpoint URL "
                        "(env: LAW_DCE_ENDPOINT). e.g. "
                        "https://dce-foo.westeurope-1.ingest.monitor.azure.com")
    p.add_argument("--workspace-dcr-id",
                   default=_env_str("LAW_DCR_IMMUTABLE_ID"),
                   help="DCR immutable ID (env: LAW_DCR_IMMUTABLE_ID). "
                        "e.g. dcr-abc123...")
    p.add_argument("--workspace-stream",
                   default=_env_str("LAW_STREAM_NAME", "Custom-DnsProbe_CL"),
                   help="DCR custom stream name (env: LAW_STREAM_NAME). "
                        "Default: Custom-DnsProbe_CL")
    args = p.parse_args(argv)
    args.duration_seconds = parse_duration(args.duration)
    return args


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    Probe(args).run()


if __name__ == "__main__":
    main()
