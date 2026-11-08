"""Synthetic but realistic incident history for "Acme Pay", a mid-size payments company.

Services: payments-api (Java/Spring, Postgres via PgBouncer), ledger-service (Kafka
consumer), checkout-web (Next.js behind an nginx ingress), fraud-scoring (Python model
server), notification-worker (Celery + Redis), on EKS with Argo CD deployments.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone


@dataclass
class Alert:
    id: str
    title: str
    service: str
    severity: str
    details: str
    fired_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Incident:
    id: str
    title: str
    service: str
    severity: str
    occurred_at: str
    responder: str
    symptoms: str
    root_cause: str
    fix_steps: list[str]
    did_not_work: list[str] = field(default_factory=list)
    minutes_to_resolve: int = 0
    follow_ups: list[str] = field(default_factory=list)

    @property
    def timestamp(self) -> datetime:
        return datetime.fromisoformat(self.occurred_at).replace(tzinfo=timezone.utc)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_memory_text(self) -> str:
        lines = [
            f"Incident {self.id} ({self.severity.upper()}) on service {self.service}: {self.title}.",
            f"Occurred {self.occurred_at} UTC. Responder: {self.responder}. Resolved in {self.minutes_to_resolve} minutes.",
            f"Symptoms and alert signals: {self.symptoms}",
            f"Root cause: {self.root_cause}",
            "Remediation that worked: " + " ".join(f"({i}) {s}" for i, s in enumerate(self.fix_steps, 1)),
        ]
        if self.did_not_work:
            lines.append("Tried and did NOT help (avoid wasting time on these): " + "; ".join(self.did_not_work) + ".")
        if self.follow_ups:
            lines.append("Post-mortem follow-ups: " + "; ".join(self.follow_ups) + ".")
        return "\n".join(lines)


HISTORICAL_INCIDENTS: list[Incident] = [
    Incident(
        id="INC-2026-0117",
        title="payments-api 5xx spike and p99 latency 6s after scaling to 24 replicas",
        service="payments-api",
        severity="sev1",
        occurred_at="2026-01-17T02:14:00",
        responder="Priya Nair",
        symptoms=(
            "Datadog monitor 'payments-api 5xx rate > 5%' fired at 18%. Logs: "
            "'HikariPool-1 - Connection is not available, request timed out after 30000ms' and "
            "'org.postgresql.util.PSQLException: FATAL: sorry, too many clients already'. "
            "PgBouncer showed cl_waiting=410."
        ),
        root_cause=(
            "HPA scaled payments-api from 12 to 24 pods during a merchant flash sale. Each pod has a "
            "Hikari maximumPoolSize of 20, so 480 client connections hit PgBouncer whose "
            "default_pool_size was 200 and max_client_conn 400. Connection starvation, not slow queries."
        ),
        fix_steps=[
            "kubectl -n payments scale deploy/payments-api --replicas=12 to stop the stampede",
            "Raised PgBouncer max_client_conn to 1000 and default_pool_size to 300 in pgbouncer.ini and ran RELOAD on the admin console",
            "Lowered Hikari maximumPoolSize to 10 via SPRING_DATASOURCE_HIKARI_MAXIMUM_POOL_SIZE and rolled out",
        ],
        did_not_work=[
            "Restarting payments-api pods (new pods immediately re-exhausted the pool)",
            "Failing over the Postgres primary (DB CPU was only 35%, it was never the bottleneck)",
        ],
        minutes_to_resolve=96,
        follow_ups=["Cap HPA maxReplicas at 16 until pool math is automated", "Alert on PgBouncer cl_waiting > 50"],
    ),
    Incident(
        id="INC-2026-0303",
        title="ledger-service consumer lag 900k on ledger.events, settlements delayed",
        service="ledger-service",
        severity="sev2",
        occurred_at="2026-03-03T09:40:00",
        responder="Arjun Mehta",
        symptoms=(
            "Burrow alert 'ledger-consumer lag > 100000' on topic ledger.events. Consumer group "
            "ledger-consumer rebalancing every ~5 minutes. Logs: 'CommitFailedException: Commit cannot be "
            "completed since the group has already rebalanced' and 'consumer poll timeout has expired'."
        ),
        root_cause=(
            "A new FX conversion call inside the message handler added ~400ms per record. With "
            "max.poll.records=500 a batch took longer than max.poll.interval.ms=300000, so the broker kicked "
            "consumers out, causing an endless rebalance storm."
        ),
        fix_steps=[
            "Set max.poll.records=100 and max.poll.interval.ms=600000 via the ledger-service ConfigMap and restarted the consumer deployment",
            "Moved the FX lookup behind a 60s in-memory cache",
            "Watched lag drain in Burrow for 25 minutes before closing",
        ],
        did_not_work=[
            "Adding more consumer pods (more members made rebalances slower and lag grew)",
            "Increasing topic partitions (not possible without reordering guarantees for ledger.events)",
        ],
        minutes_to_resolve=71,
        follow_ups=["Add per-record handler latency histogram", "Load test handlers against max.poll.interval budget"],
    ),
    Incident(
        id="INC-2026-0412",
        title="payments-api connection pool exhaustion after deploy v2.31.0",
        service="payments-api",
        severity="sev1",
        occurred_at="2026-04-12T14:05:00",
        responder="Daniel Okafor",
        symptoms=(
            "5xx rate 22% and p99 4.8s within 6 minutes of Argo CD sync of v2.31.0. Logs: "
            "'HikariPool-1 - Connection is not available, request timed out after 30000ms'. "
            "pg_stat_activity showed 180 sessions 'idle in transaction'."
        ),
        root_cause=(
            "v2.31.0 added @Transactional to RefundService.listRefunds which made an outbound HTTP call to the "
            "card network inside the transaction, holding DB connections idle-in-transaction for seconds."
        ),
        fix_steps=[
            "argocd app rollback payments-api to v2.30.4 (5xx recovered within 3 minutes)",
            "Ran SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE state='idle in transaction' AND now()-state_change > interval '60 seconds' to free stuck sessions",
            "Fixed forward in v2.31.1 by moving the HTTP call outside the transaction",
        ],
        did_not_work=[
            "Raising Hikari pool size to 40 (just moved the exhaustion to PgBouncer)",
        ],
        minutes_to_resolve=38,
        follow_ups=["Set idle_in_transaction_session_timeout=30s on payments DB", "Lint rule: no outbound HTTP inside @Transactional"],
    ),
    Incident(
        id="INC-2026-0521",
        title="notification-worker Redis OOM, password reset emails not sent",
        service="notification-worker",
        severity="sev2",
        occurred_at="2026-05-21T19:22:00",
        responder="Sneha Reddy",
        symptoms=(
            "Celery logs: 'redis.exceptions.ResponseError: OOM command not allowed when used memory > maxmemory'. "
            "ElastiCache redis-notify memory 100%, evictions 0 (policy noeviction). Queue depth 2.3M."
        ),
        root_cause=(
            "A marketing campaign enqueued 2M emails into the same Redis used for transactional email; "
            "results backend also stored every task result with no expiry."
        ),
        fix_steps=[
            "Purged the marketing queue: celery -A notify purge -Q marketing -f",
            "Set result_expires=3600 and CELERY_TASK_IGNORE_RESULT=True for fire-and-forget tasks",
            "Moved marketing sends to a separate queue and worker pool with rate limit 200/s",
        ],
        did_not_work=["Scaling the ElastiCache node vertically (took 25 min and refilled within 10)"],
        minutes_to_resolve=64,
        follow_ups=["Separate Redis for transactional vs bulk", "Alert on Redis memory > 75%"],
    ),
    Incident(
        id="INC-2026-0608",
        title="fraud-scoring timeouts causing checkout declines",
        service="fraud-scoring",
        severity="sev1",
        occurred_at="2026-06-08T11:47:00",
        responder="Priya Nair",
        symptoms=(
            "payments-api logs 'FraudClient timeout after 800ms, failing closed'. Decline rate jumped from 3% to 27%. "
            "fraud-scoring pods at 98% CPU, model v14 deployed 40 minutes earlier."
        ),
        root_cause=(
            "Model v14 had a feature that ran a 12-month aggregate query per request instead of reading the "
            "precomputed feature store; p99 went from 60ms to 1.9s."
        ),
        fix_steps=[
            "Rolled the model alias back: mlflow models alias set fraud-model production --version 13",
            "Temporarily switched FraudClient to fail-open for transactions under $50 via feature flag fraud.fail_open_small_tx",
            "Restored fail-closed after p99 < 100ms for 15 minutes",
        ],
        did_not_work=["Scaling fraud-scoring to 3x pods (CPU-bound per request, latency unchanged)"],
        minutes_to_resolve=33,
        follow_ups=["Shadow-score new models for 24h before promotion", "Latency SLO gate in model CI"],
    ),
    Incident(
        id="INC-2026-0719",
        title="payments-api 502s from ingress after deploy v2.38.0, pods crashlooping",
        service="payments-api",
        severity="sev1",
        occurred_at="2026-07-19T06:31:00",
        responder="Arjun Mehta",
        symptoms=(
            "nginx ingress 502 rate 30%. Pods in CrashLoopBackOff with 'Liveness probe failed: HTTP probe failed "
            "with statuscode: 503'. JVM startup took ~95s after adding the new Flyway migrations check."
        ),
        root_cause=(
            "Liveness probe initialDelaySeconds was 60 while startup now took ~95s, so kubelet killed pods before "
            "they were ready. Not a DB problem even though the logs mentioned Hikari during startup."
        ),
        fix_steps=[
            "argocd app rollback payments-api to v2.37.2",
            "Added a startupProbe (failureThreshold 30, periodSeconds 5) and kept the liveness probe unchanged",
            "Redeployed v2.38.1 with the startupProbe",
        ],
        did_not_work=["Investigating PgBouncer and Hikari for 20 minutes (red herring)"],
        minutes_to_resolve=52,
        follow_ups=["Helm chart default startupProbe for all JVM services"],
    ),
    Incident(
        id="INC-2026-0814",
        title="checkout-web elevated 499s and slow page loads in India region",
        service="checkout-web",
        severity="sev3",
        occurred_at="2026-08-14T15:10:00",
        responder="Sneha Reddy",
        symptoms="CloudFront 4xx rate up, client 499s in nginx logs, RUM LCP p75 7s for ap-south-1 users only.",
        root_cause="A CloudFront cache behavior change set TTL=0 for /_next/static/*, so every asset request hit origin in us-east-1.",
        fix_steps=[
            "Reverted the CloudFront behavior via Terraform (cache policy Managed-CachingOptimized for /_next/static/*)",
            "Invalidated /_next/static/* after revert",
        ],
        did_not_work=["Scaling checkout-web pods (origin was healthy)"],
        minutes_to_resolve=45,
        follow_ups=["Terraform plan review required for CDN cache policies"],
    ),
    Incident(
        id="INC-2026-0902",
        title="ledger-service stuck on poison message, consumer lag growing",
        service="ledger-service",
        severity="sev2",
        occurred_at="2026-09-02T23:55:00",
        responder="Daniel Okafor",
        symptoms=(
            "Lag on partition 7 of ledger.events only, other partitions fine. Logs repeating "
            "'JsonMappingException: Cannot deserialize value of type BigDecimal from String \"NaN\"' at offset 88412021."
        ),
        root_cause="An upstream partner sent amount='NaN'; the consumer retried the same offset forever with no dead-letter queue.",
        fix_steps=[
            "Skipped the bad offset: kafka-consumer-groups --bootstrap-server $BROKERS --group ledger-consumer --topic ledger.events:7 --reset-offsets --to-offset 88412022 --execute (consumer scaled to 0 first)",
            "Published the raw message to ledger.events.dlq for finance review",
            "Shipped DLQ handling with max 3 retries",
        ],
        did_not_work=["Restarting consumers (re-read the same poison offset)", "Tuning max.poll settings (not a rebalance issue this time)"],
        minutes_to_resolve=58,
        follow_ups=["DLQ for every consumer", "Validate partner payload schema at the gateway"],
    ),
    Incident(
        id="INC-2026-1011",
        title="payments-api 5xx after Postgres minor version upgrade, prepared statement errors",
        service="payments-api",
        severity="sev2",
        occurred_at="2026-10-11T03:20:00",
        responder="Priya Nair",
        symptoms="Logs: 'ERROR: prepared statement \"S_12\" already exists' and 'cached plan must not change result type' after RDS maintenance window.",
        root_cause="PgBouncer in transaction pooling mode with server-side prepared statements enabled in the JDBC driver; RDS restart reshuffled server connections.",
        fix_steps=[
            "Set prepareThreshold=0 on the JDBC URL to disable server-side prepared statements and rolled out",
            "Ran RECONNECT on the PgBouncer admin console to drop stale server connections",
        ],
        did_not_work=["Rolling back the application (issue was in the connection layer, not the code)"],
        minutes_to_resolve=41,
        follow_ups=["Document PgBouncer transaction-mode JDBC settings in the service template"],
    ),
]


DEMO_ALERTS: list[Alert] = [
    Alert(
        id="ALERT-7781",
        title="payments-api 5xx rate 19% and p99 latency 4.6s after Argo CD sync of v2.41.0",
        service="payments-api",
        severity="sev1",
        details=(
            "Monitor: payments-api 5xx rate > 5% (19.2%). Started 4 minutes after deploy v2.41.0.\n"
            "Sample logs:\n"
            "  ERROR c.a.p.RefundController - HikariPool-1 - Connection is not available, request timed out after 30000ms\n"
            "  WARN  o.h.e.j.s.SqlExceptionHelper - SQL Error: 0, SQLState: 08003\n"
            "pg_stat_activity: 160 sessions in state 'idle in transaction'."
        ),
    ),
    Alert(
        id="ALERT-7802",
        title="ledger-service consumer lag 1.2M on ledger.events, group rebalancing repeatedly",
        service="ledger-service",
        severity="sev2",
        details=(
            "Burrow: ledger-consumer lag 1,204,331 and rising on all partitions.\n"
            "Logs: CommitFailedException: Commit cannot be completed since the group has already rebalanced.\n"
            "Deploy 2 hours ago added a merchant-risk enrichment call in LedgerEventHandler."
        ),
    ),
    Alert(
        id="ALERT-7815",
        title="checkout-web TLS handshake failures to api.acmepay.com, x509 certificate has expired",
        service="checkout-web",
        severity="sev1",
        details=(
            "Synthetic check 'checkout happy path' failing from all regions.\n"
            "curl: (60) SSL certificate problem: certificate has expired\n"
            "cert-manager: Certificate api-acmepay-tls Ready=False, reason: Failed, "
            "message: 'Failed to finalize order: 403 urn:ietf:params:acme:error:unauthorized'."
        ),
    ),
    Alert(
        id="ALERT-7840",
        title="fraud-scoring x509 certificate has expired calling internal feature-store",
        service="fraud-scoring",
        severity="sev2",
        details=(
            "fraud-scoring logs: ssl.SSLCertVerificationError: certificate verify failed: certificate has expired.\n"
            "cert-manager Certificate feature-store-tls Ready=False, ACME order failed with 403 unauthorized.\n"
            "Checkout falling back to fail-closed declines."
        ),
    ),
]

# What the on-call actually did for ALERT-7815, used by the guided demo to teach the agent
# something it has never seen, so the follow-up ALERT-7840 can recall it.
DEMO_NEW_RESOLUTION = Incident(
    id="INC-2026-1108",
    title="checkout-web outage: api.acmepay.com TLS cert expired, cert-manager ACME HTTP-01 challenge blocked",
    service="checkout-web",
    severity="sev1",
    occurred_at="2026-11-08T08:40:00",
    responder="You (on-call)",
    symptoms=(
        "TLS handshake failures, 'certificate has expired'. cert-manager Certificate Ready=False with ACME 403 "
        "unauthorized while finalizing the order."
    ),
    root_cause=(
        "A WAF rule added last week blocked /.well-known/acme-challenge/* so Let's Encrypt HTTP-01 validation failed "
        "and the cert silently stopped renewing 30 days ago."
    ),
    fix_steps=[
        "Added a WAF allow rule for path /.well-known/acme-challenge/*",
        "Forced renewal: cmctl renew api-acmepay-tls -n ingress",
        "Verified with: kubectl get certificate -n ingress and openssl s_client -connect api.acmepay.com:443 | openssl x509 -noout -dates",
    ],
    did_not_work=["Restarting the nginx ingress controller (it kept serving the expired secret)"],
    minutes_to_resolve=47,
    follow_ups=["Alert when any Certificate is within 14 days of expiry", "WAF change review must check ACME paths"],
)


def incident_tags(service: str, severity: str, kind: str = "incident") -> list[str]:
    return [f"service:{service}", f"severity:{severity}", f"kind:{kind}"]
