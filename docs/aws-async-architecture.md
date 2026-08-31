# Stage 9.5 asynchronous AWS architecture

Stage 9.5 replaces the single-host runtime with separately deployable API,
worker, and controlled downstream-simulator roles. This note describes the
infrastructure boundary currently defined in Terraform. Task definitions and
services are the next increment, so this configuration is not yet ready for
cloud session 3.

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
                                      ^
                                      |
                              worker and migration tasks
```

## Cost-bounded network boundary

The load balancer and Fargate tasks use two public subnets in different
Availability Zones. The later ECS services will assign public IP addresses to
their task ENIs so they can pull ECR images and reach SQS, Secrets Manager, and
CloudWatch without a NAT gateway or multiple billed interface endpoints. AWS
documents this as a supported Fargate networking mode. It is an explicit
short-lived experiment tradeoff, not the preferred default for a long-lived
production system.

Public addressing does not imply public task ingress. The API security group
accepts port `8000` only from the load balancer security group. Worker tasks
have no ingress rules. Simulator tasks accept port `8001` only from the worker
security group. RDS remains in isolated subnets and accepts PostgreSQL only
from the synchronous rehost, asynchronous API/migration, and worker security
groups. The public load balancer accepts HTTP only from the approved benchmark
IPv4 `/32` and may send only TCP `8000` inside the experiment VPC.

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
to the one RDS-managed credential secret; the task definitions will decide
which containers receive database secret fields.

The source queue uses SQS-managed encryption, 20-second long polling, a
120-second visibility timeout, one-day retention, and five receives before
redrive. Its encrypted DLQ retains messages for four days and accepts redrive
only from that exact source queue.

## Load balancing and observability foundation

The Application Load Balancer targets task IPs and checks `/health/ready`, so
an API process is routable only while it can reach PostgreSQL. Invalid HTTP
header fields are dropped and deletion protection remains disabled so bounded
session teardown cannot be blocked.

The ECS cluster enables Container Insights. API, migration, simulator, and
worker processes each have a dedicated CloudWatch log group with one-day
retention and no retain-on-destroy behavior. Task definitions will attach the
`awslogs` driver in the next increment. Application metrics, ALB metrics, SQS
depth and age, alarms, and the experiment dashboard remain separate work.

## Teardown boundary

Every resource inherits the session tags. Native teardown verification now
checks the exact Application Load Balancer, target group, ECS cluster, all ECS
and rehost IAM roles, all service ECR repositories, both SQS queues, every
session log group, the RDS resources and managed secret, and the underlying EC2
network resources. Listener deletion is implied by authoritative load-balancer
absence because a listener cannot exist independently of its load balancer.

No Stage 9.5 infrastructure has been applied to AWS. Cloud session 3 still
requires a complete task/service definition, a reviewed resource and cost
proposal, explicit authorization, and unconditional verified teardown.

## References

- [Amazon ECS outbound networking](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/networking-outbound.html)
- [Amazon ECS Fargate task networking](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/fargate-task-networking.html)
- [Send Amazon ECS logs to CloudWatch](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/using_awslogs.html)
- [Application Load Balancer Terraform resource](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/lb)
- [Amazon SQS dead-letter queues](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-dead-letter-queues.html)
