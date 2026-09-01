# Stage 9.5 asynchronous AWS architecture

Stage 9.5 replaces the single-host runtime with separately deployable API,
worker, and controlled downstream-simulator roles. This note describes the
infrastructure boundary currently defined in Terraform, the guarded
multi-phase workflow that deploys it, and the tiny integration runner that
closes cloud session 3. No resource is applied until the separately reviewed
session proposal receives explicit authorization.

```text
approved benchmark /32
          |
          | HTTP 80
          v
public Application Load Balancer
          |
          | TCP 8000
          v
fixed-capacity API tasks ----> encrypted SQS delivery queue ----> worker tasks
          |                                |                           |
          |                                `----> encrypted DLQ         | TCP 8001
          |                                                            v
          +--------------------> private RDS                 simulator tasks
                                      ^                           ^
                                      |                           |
                              worker and migration tasks --- private DNS
```

## Cost-bounded network boundary

The load balancer and Fargate tasks use two public subnets in different
Availability Zones. Every ECS service assigns a public IP address to its task
ENI so it can pull ECR images and reach SQS, Secrets Manager, and CloudWatch
without a NAT gateway or multiple billed interface endpoints. AWS documents
this as a supported Fargate networking mode. It is an explicit short-lived
experiment tradeoff, not the preferred default for a long-lived production
system.

Public addressing does not imply public task ingress. The API security group
accepts port `8000` only from the load balancer security group. Worker tasks
have no ingress rules. Simulator tasks accept port `8001` only from the worker
security group. RDS remains in isolated subnets and, in `async` deployment
mode, accepts PostgreSQL only from the asynchronous API, migration, and worker
security groups. The mutually exclusive `rehost` mode instead permits only its
EC2 host. Migration tasks have their own no-ingress security group. The public
load balancer accepts HTTP only from the approved benchmark IPv4 `/32` and may
send only TCP `8000` inside the experiment VPC.

The listener intentionally uses HTTP rather than introducing a public domain,
certificate, and Route 53 boundary for synthetic short-lived experiments. A
real public deployment must terminate HTTPS. The benchmark source restriction
is defense in depth for this controlled environment, not a substitute for TLS
for real customer data.

## Queue and identity boundaries

The API task role can only send to the delivery queue. The worker task role can
send, receive, inspect, and delete messages on that exact source queue. The
simulator and migration task roles receive no queue permissions. The common ECS
execution role has the standard image-pull and log-delivery policy plus access
to the one RDS-managed credential secret. API, worker, and migration task
definitions receive only its `password` field through ECS secret injection;
the RDS hostname, port, database, and username are non-secret environment
values. TrackRelay constructs the URL inside the process, URL-encodes the
credential, and requires PostgreSQL TLS, so Terraform never assembles or stores
a plaintext database URL.

The source queue uses SQS-managed encryption, 20-second long polling, a
120-second visibility timeout, one-day retention, and five receives before
redrive. Its encrypted DLQ retains messages for four days and accepts redrive
only from that exact source queue.

## Runtime and deployment boundary

API, worker, simulator, and migration task definitions require all three image
digests together and reference ECR only as `repository@sha256:...`. All run as
Linux x86_64 Fargate tasks under UID/GID `10001`, with a read-only root
filesystem, an init process, only `/tmp` writable, a 30-second stop timeout,
and the role-specific one-day `awslogs` group. Each API task receives 1 vCPU
and 2 GiB; worker, simulator, and migration each receive 0.25 vCPU and 0.5 GiB.

The migration definition runs only `alembic upgrade head`; it is not an ECS
service. The API, worker, and simulator service resources are independently
gated by `async_services_enabled`. The API maintains exactly two tasks while
worker and simulator each maintain one on Fargate platform `1.4.0`. No API
Application Auto Scaling target or policy exists. Their zero-to-100-percent
replacement configuration prevents temporary task-count increases at the cost
of acceptable pre-experiment downtime. Deployment circuit breakers roll back
failed task replacements.

The API reservation is intentionally fixed at an aggregate 2 vCPU and 4 GiB,
eight times the CPU and memory assigned to the minimum worker. Two replicas
also allow ECS to balance ingestion replicas across the two Fargate
Availability Zones instead of relying on one oversized process. This is a
capacity candidate, not a cloud measurement. Before the Stage 9.6 control is
frozen, the selected peak workload must keep both API tasks running, preserve
the 500 ms p95, 1% error, request-completeness, and correctness guardrails, and
keep maximum API and simulator CPU and memory below 70%. RDS CPU, connections,
memory, I/O, and application pool evidence must also retain headroom. The
workload must exceed one-worker delivery capacity. Failure means re-freezing
the limiting non-worker tier before either causal treatment, never resizing it
between fixed and elastic runs.

The required deployment order is deliberate:

1. select `deployment_mode=async`, which excludes the historical EC2 host, then
   create the shared and asynchronous foundations with no image digests and
   services disabled;
2. publish all three images, then register all four immutable task definitions
   with all three digests and services still disabled;
3. run the exported migration task in its dedicated security group and require
   a successful exit; and
4. enable the three fixed services only after that success.

The lifecycle manifest freezes the selected deployment mode, and plan tests
prove that the two runtime topologies cannot be created together. After an
approved foundation apply, `make aws-async-deploy` performs the remaining
sequence. It publishes all three targets under the approved Git tag, records
only their immutable digests, saves and hashes the runtime plan, applies that
exact plan with services disabled, runs and waits for the migration task,
refuses to continue unless it exits zero, saves and applies a separate service
plan, and requires two stable API tasks plus one stable worker and simulator
task. The controller cross-checks Terraform's non-secret capacity output
against its frozen contract and records names, desired counts, CPU, and memory
in lifecycle evidence. A normal error, `SIGINT`, or `SIGTERM` after arming
first stops any outstanding standalone migration task, then triggers Terraform
destroy followed by native verification. Successful convergence deliberately
leaves the stack running for the small integration checkpoint.

## Load balancing, discovery, and observability

The Application Load Balancer targets task IPs and checks `/health/ready`, so
an API process is routable only while it can reach PostgreSQL. Invalid HTTP
header fields are dropped and deletion protection remains disabled so bounded
session teardown cannot be blocked.

The simulator registers its replaceable task IP in a session-specific private
Cloud Map DNS namespace. The worker calls
`simulator.<session-hash>.internal:8001`; no simulator address enters Terraform
variables or experiment configuration. The namespace creates a billed Route 53
private hosted zone, so the cloud-session estimate must include it and teardown
must remove the namespace. Simulator ingress accepts the worker delivery path
and the API's read-only reconciliation path, but no public source. The
`/32`-restricted API registers and completes synthetic test-run manifests and
returns only their database summary or reconciled evidence; RDS and the
simulator remain private.

The ECS cluster enables Container Insights. API, migration, simulator, and
worker processes each use a dedicated CloudWatch log group with one-day
retention and no retain-on-destroy behavior. A session-specific, destroyable
CloudWatch dashboard fixes every AWS service series at the native 60-second
period:

- observed ALB requests per second, derived from target-selected requests plus
  load-balancer 4xx and 5xx responses;
- ALB target-response p95 in seconds with the frozen 500 ms SLO line;
- target and load-balancer 4xx/5xx responses as a percentage of observed ALB
  requests, with the frozen 1% SLO line;
- ECS Container Insights `RunningTaskCount` for the exact worker service;
- conservative unfinished source-queue work, calculated from the maximum
  visible, in-flight, and delayed SQS counts, alongside visible DLQ messages;
- the maximum age of the oldest source-queue message in seconds; and
- maximum API and simulator service CPU and memory utilization with the 70%
  headroom qualification ceiling.

These service metrics are operational approximations. ALB metrics exclude
health checks, are sparse without traffic, and `RequestCount` includes only
requests for which a target was selected. SQS depth metrics are approximate,
and summing per-period maxima can overstate simultaneous work. Therefore the
load driver's scheduled rate remains the authoritative offered load, and the
database, queue-attribute, and simulator reconciliation evidence remains the
authoritative completion and drain proof. The dashboard makes the aligned
elasticity story visible; it does not replace experiment guardrails. No paging
alarms are created for this short-lived synthetic environment.

After successful service convergence, `make aws-async-integration` executes
exact 1-, 10-, and 100-event **total batches** from the approved benchmark
location. These values are event counts, not scheduled events/s, and this
checkpoint makes no capacity claim. The rate-controlled performance ladder is
reserved for the Stage 9.6/9.7 experiment.
For each run it saves the input and request evidence as it proceeds, then
samples exact database/outbox counts and approximate SQS/DLQ attributes every
10 seconds. A run passes only when ingestion p95 is below 500 ms, errors are
below 1%, every event reconciles through the private simulator without a
duplicate business effect or incorrect final shipment, work first drains no
later than 120 seconds, and the empty state remains continuously observed for
180 seconds. Before teardown, the runner also requires the seven-widget
dashboard and published native series for ALB requests, API and simulator CPU,
worker running tasks, SQS sends, and RDS CPU. A normal failure, success,
`SIGINT`, or `SIGTERM` reaches unconditional destroy and native absence
verification.

## Teardown boundary

Every taggable resource inherits the session tags; the globally scoped
dashboard instead uses the exact session-derived name. Native teardown now
checks the exact Application Load Balancer, target group, ECS cluster,
pending/running tasks and services, active task-definition family, private
Cloud Map namespace, all ECS and rehost IAM roles, all service ECR
repositories, both SQS queues, every
session log group, the exact session dashboard, the RDS resources and managed
secret, and the underlying EC2 network resources. Listener deletion is implied
by authoritative load-balancer absence because a listener cannot exist
independently of its load balancer; Cloud Map namespace absence likewise
implies that its service and managed private hosted zone are gone. Deregistered
inactive task-definition revisions are non-runnable control-plane history, so
the authoritative safety check is that no session family remains `ACTIVE`.

No Stage 9.5 infrastructure has been applied to AWS. Cloud session 3 now needs
only a reviewed resource and cost proposal, explicit authorization, execution
of the locally tested deployment and integration controllers, and review of
their collected evidence plus verified teardown.

## References

- [Stage 9.5 operator runbook](aws-async-integration-runbook.md)
- [Stage 9.6/9.7 elasticity experiment](aws-elasticity-experiment.md)

- [Amazon ECS outbound networking](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/networking-outbound.html)
- [Amazon ECS Fargate task networking](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/fargate-task-networking.html)
- [Amazon ECS Fargate task CPU and memory combinations](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-cpu-memory-error.html)
- [Amazon ECS service utilization metrics](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/service_utilization.html)
- [Amazon ECS standalone tasks](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/standalone-tasks.html)
- [Amazon ECS private DNS service discovery](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/service-discovery.html)
- [Inject a Secrets Manager JSON key into an ECS task](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/secrets-envvar-secrets-manager.html)
- [Send Amazon ECS logs to CloudWatch](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/using_awslogs.html)
- [CloudWatch metrics for an Application Load Balancer](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/load-balancer-cloudwatch-metrics.html)
- [Amazon ECS Container Insights metrics](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Container-Insights-enhanced-observability-metrics-ECS.html)
- [Available CloudWatch metrics for Amazon SQS](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-available-cloudwatch-metrics.html)
- [CloudWatch dashboard body syntax](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-Dashboard-Body-Structure.html)
- [Application Load Balancer Terraform resource](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/lb)
- [Amazon SQS dead-letter queues](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-dead-letter-queues.html)
