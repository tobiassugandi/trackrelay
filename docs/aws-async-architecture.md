# Stage 9.5 asynchronous AWS architecture

Stage 9.5 replaces the single-host runtime with separately deployable API,
worker, and controlled downstream-simulator roles. This note describes the
infrastructure boundary currently defined in Terraform. Its guarded multi-phase
deployment workflow and experiment metrics remain unfinished, so this
configuration is not yet ready for cloud session 3.

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
security group. RDS remains in isolated subnets and accepts PostgreSQL only
from the synchronous rehost, asynchronous API, migration, and worker security
groups. Migration tasks have their own no-ingress security group. The public
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
and the role-specific one-day `awslogs` group. The API receives `0.5` vCPU and
1 GiB; worker, simulator, and migration each receive `0.25` vCPU and 0.5 GiB.

The migration definition runs only `alembic upgrade head`; it is not an ECS
service. The API, worker, and simulator service resources are independently
gated by `async_services_enabled` and each maintains exactly one task on
Fargate platform `1.4.0`. Their zero-to-100-percent replacement configuration
prevents temporary task-count doubling at the cost of acceptable pre-experiment
downtime. Deployment circuit breakers roll back failed task replacements.

The required deployment order is deliberate:

1. create the repositories and infrastructure with no image digests and
   services disabled;
2. publish all three images, then register all four immutable task definitions
   with all three digests and services still disabled;
3. run the exported migration task in its dedicated security group and require
   a successful exit; and
4. enable the three fixed services only after that success.

The existing single-plan cloud-session command does not yet automate this
sequence. Until guarded multi-phase automation records each immutable plan,
digest, migration result, and service convergence, the runtime must not be
applied to AWS.

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
must remove the namespace.

The ECS cluster enables Container Insights. API, migration, simulator, and
worker processes each use a dedicated CloudWatch log group with one-day
retention and no retain-on-destroy behavior. Application metrics, ALB metrics,
SQS depth and age, alarms, and the experiment dashboard remain separate work.

## Teardown boundary

Every resource inherits the session tags. Native teardown verification now
checks the exact Application Load Balancer, target group, ECS cluster and
services, active task-definition family, private Cloud Map namespace, all ECS
and rehost IAM roles, all service ECR repositories, both SQS queues, every
session log group, the RDS resources and managed secret, and the underlying EC2
network resources. Listener deletion is implied by authoritative load-balancer
absence because a listener cannot exist independently of its load balancer;
Cloud Map namespace absence likewise implies that its service and managed
private hosted zone are gone. Deregistered inactive task-definition revisions
are non-runnable control-plane history, so the authoritative safety check is
that no session family remains `ACTIVE`.

No Stage 9.5 infrastructure has been applied to AWS. Cloud session 3 still
requires guarded multi-phase deployment automation, experiment metrics, a
reviewed resource and cost proposal, explicit authorization, and unconditional
verified teardown.

## References

- [Amazon ECS outbound networking](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/networking-outbound.html)
- [Amazon ECS Fargate task networking](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/fargate-task-networking.html)
- [Amazon ECS standalone tasks](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/standalone-tasks.html)
- [Amazon ECS private DNS service discovery](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/service-discovery.html)
- [Inject a Secrets Manager JSON key into an ECS task](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/secrets-envvar-secrets-manager.html)
- [Send Amazon ECS logs to CloudWatch](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/using_awslogs.html)
- [Application Load Balancer Terraform resource](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/lb)
- [Amazon SQS dead-letter queues](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-dead-letter-queues.html)
