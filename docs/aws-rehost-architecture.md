# Stage 9.1 synchronous rehost architecture

Stage 9.1 is a portability checkpoint for the unchanged synchronous TrackRelay application. It is not the final modern architecture and it is not the fixed-versus-elastic causal experiment.

```text
approved benchmark host
        |
        | TCP 8000 from one IPv4 /32
        v
public subnet: one t4g.small EC2 instance
        |- TrackRelay API container
        |- one-off migration container
        |- PostgreSQL container
        `- downstream simulator container

EC2 pulls the API image from ECR and is administered through SSM, not SSH.
```

## What Terraform defines now

The module defines one dedicated VPC, one public subnet and route, an internet gateway, a restricted security group, one ECR repository, the minimum EC2 IAM role and instance profile, and one ARM Amazon Linux 2023 `t4g.small` host. The instance uses standard CPU credits and one encrypted 16 GiB gp3 root volume that is deleted on termination.

The module deliberately has no NAT gateway, load balancer, Elastic IP, SSH key, snapshot, or RDS instance. The instance receives a temporary public IPv4 so it can reach ECR and SSM without a chargeable NAT gateway. Only port `8000` is reachable, and only from the benchmark location's explicitly supplied `/32`; port `22` is closed. IMDSv2 tokens are required.

Amazon EC2 T4g is available in Asia Pacific (Jakarta), and the selected Amazon Linux 2023 ARM image supports the architecture. AWS documents that the SSM Agent is commonly preinstalled on Amazon Linux 2023 images; bootstrap also enables it explicitly.

## Host runtime

`deploy/rehost/compose.yaml` is the source of truth for the synchronous host runtime. One application image supplies the migration, API, and downstream-simulator processes. PostgreSQL is pinned to a multi-platform image digest. The database and simulator are internal-only; the API is the only service with a published host port.

Compose waits for PostgreSQL health before running `alembic upgrade head`, and the API waits for both a successful migration and a healthy simulator. Application containers have read-only root filesystems, bounded JSON logs, and a small temporary filesystem. PostgreSQL data uses a named volume so an application-container restart does not silently erase it. The data remains synthetic and the complete volume is disposable at session teardown.

Runtime values come from a private, uncommitted env file. The committed example binds the API to `127.0.0.1` for local validation. A cloud session must explicitly bind the host port to `0.0.0.0`; the EC2 security group still restricts the external source to the approved `/32`.

## What remains local-only

`make infra-check` formats and validates Terraform and executes provider-mocked plan assertions. Those assertions guard the chosen instance type, standard CPU credits, volume size and deletion, IMDSv2, ingress restriction, and removable ECR repository without contacting AWS.

`make rehost-smoke` exercises the complete runtime locally in an isolated Compose project. It verifies migrations, readiness, non-root processes, ingestion, downstream delivery, and durable PostgreSQL state across a restart, then removes its containers, network, and volume even on failure.

The frozen-workload controller is also prepared locally. `make aws-rehost-workload` runs the exact Step 8.6 healthy workload rates from the approved developer machine, not from the small EC2 host. For every rate it generates the manifest locally, uses SSM to regenerate and prepare the same run inside the private Compose network, drives the public API with local k6, samples API runtime metrics, then asks the private helper to reconcile PostgreSQL and simulator evidence. PostgreSQL and the simulator never receive public ports.

The ignored session bundle records the workload contract, driver placement, region, instance type, process counts, database placement, manifests, k6 summaries, runtime samples, compact reconciliation, and per-rate pass interpretation. It deliberately omits the temporary API address, instance ID, AWS account ID, and ECR repository URL from the workload result files. This is migration and benchmark-portability evidence, not the final fixed-versus-elastic causal comparison.

Stage 9.2 will replace host-local PostgreSQL with RDS for the target AWS deployment. A real AWS plan waits until Stage 9.2 is locally prepared, the private CLI session is authenticated, and the account owner approves cloud session 1's exact resource list, estimate, duration, and cost ceiling.

## Image publication and deployment controller

`make aws-rehost-publish` is available only after the guarded Terraform apply. It requires the clean Git revision recorded by the approved plan, builds `linux/arm64`, pushes a Git-revision tag to the session ECR repository, reads the registry digest back, logs Docker out, and saves only the architecture, tag, and digest in session evidence. The ECR password is passed through standard input and is never placed in arguments, output, or evidence. Deployment subsequently uses the immutable digest, not the mutable tag.

`make aws-rehost-deploy` waits for the specific instance's SSM agent, sends the committed Compose and installer files through `AWS-RunShellScript`, waits for success, and records only the command ID and outcome. It never opens SSH. AWS warns that Run Command parameters are retained in command history and can be recorded by CloudTrail, so the payload contains no password. The installer generates the synthetic PostgreSQL password on the instance and stores its env file with mode `0600`.

The bootstrap installs Docker Compose v2.32.4 for ARM64 from Docker's official release and verifies its published SHA-256 checksum before installation. The remote command waits for cloud-init, authenticates to ECR with the instance role, starts the digest-pinned application, verifies migration head and readiness, and sends one unique Courier Alpha event through the database and simulator. These workflows are locally unit-tested definitions only; they have not been run against AWS.

`make aws-rehost-workload` has the same revision guard and additionally requires a successfully deployed rehost. Its SSM payload contains only synthetic workload parameters and paths to the host-private Compose configuration. The external driver learns the temporary API address in memory from Terraform output, but no workload result artifact persists it. A k6 threshold failure is still reconciled so that a failing point can be interpreted; infrastructure teardown remains a separate unconditional command.

No AWS resources were created while preparing this architecture.

## Chargeable footprint when eventually applied

The planned Stage 9.1-only footprint is one `t4g.small` instance, 16 GiB of gp3 storage, one public IPv4 while the instance runs, and ECR image storage. The VPC components, IAM objects, and security group are not themselves the intended billable capacity. Stage 9.2 will add RDS and will require a revised resource list and estimate before approval.

Teardown is not considered complete merely because Terraform destroy succeeds. `make aws-verify-down` also checks empty session tags and native EC2, EBS, network, ECR, and IAM inventories. Later AWS resource types must extend that verifier before they are used.

## References

- [Amazon EC2 T4g availability in Asia Pacific (Jakarta)](https://aws.amazon.com/about-aws/whats-new/2023/06/amazon-ec2-t4g-instances-additional-regions/)
- [AWS Systems Manager Agent on Amazon Machine Images](https://docs.aws.amazon.com/systems-manager/latest/userguide/ami-preinstalled-agent.html)
- [Terraform provider mocking and tests](https://developer.hashicorp.com/terraform/language/tests/mocking)
- [AWS Systems Manager Run Command security guidance](https://docs.aws.amazon.com/systems-manager/latest/userguide/running-commands.html)
- [Docker Compose plugin installation on Linux](https://docs.docker.com/compose/install/linux/)
