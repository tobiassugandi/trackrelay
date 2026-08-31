# Stage 9.1 synchronous rehost architecture

Stage 9.1 was a portability checkpoint for the unchanged synchronous TrackRelay application. Its first successful cloud session used ARM64 on `t4g.small`. The reusable module has since moved to x86_64 so the free-plan-compatible Stage 9.3 ladder can resize one host through `t3.small`, `c7i-flex.large`, and `m7i-flex.large`. Neither version is the final modern architecture or the fixed-versus-elastic causal experiment.

```text
approved benchmark host
        |
        | TCP 8000 from one IPv4 /32
        v
public subnet: one t3.small EC2 instance
        |- TrackRelay API container
        |- one-off migration container
        |- PostgreSQL container
        `- downstream simulator container

EC2 pulls the API image from ECR and is administered through SSM, not SSH.
```

## What the rehost Terraform slice defines

The Stage 9.1 rehost slice defines one dedicated VPC, one public subnet and route, an internet gateway, a restricted security group, the API ECR repository, the minimum EC2 IAM role and instance profile, and one x86_64 Amazon Linux 2023 `t3.small` host. The instance uses standard CPU credits and one encrypted 16 GiB gp3 root volume that is deleted on termination. The shared root module now also contains the later RDS layer and Stage 9.5 worker/simulator registries and delivery queues; those additions do not change this synchronous host runtime.

The rehost slice deliberately has no NAT gateway, load balancer, Elastic IP, SSH key, or snapshot. Its later RDS instance remains private and separately described below. The instance receives a temporary public IPv4 so it can reach ECR and SSM without a chargeable NAT gateway. Only port `8000` is reachable, and only from the benchmark location's explicitly supplied `/32`; port `22` is closed. IMDSv2 tokens are required.

The three current x86_64 instance types are regionally offered in Asia Pacific (Jakarta), and the selected Amazon Linux 2023 x86_64 image is compatible with all three. AWS documents that the SSM Agent is commonly preinstalled on Amazon Linux 2023 images; bootstrap also enables it explicitly.

## Host runtime

`deploy/rehost/compose.yaml` is the source of truth for the synchronous host runtime. One application image supplies the migration, API, and downstream-simulator processes. PostgreSQL is pinned to a multi-platform image digest. The database and simulator are internal-only; the API is the only service with a published host port.

Compose waits for PostgreSQL health before running `alembic upgrade head`, and the API waits for both a successful migration and a healthy simulator. Application containers have read-only root filesystems, bounded JSON logs, and a small temporary filesystem. PostgreSQL data uses a named volume so an application-container restart does not silently erase it. The data remains synthetic and the complete volume is disposable at session teardown.

Runtime values come from a private, uncommitted env file. The committed example binds the API to `127.0.0.1` for local validation. A cloud session must explicitly bind the host port to `0.0.0.0`; the EC2 security group still restricts the external source to the approved `/32`.

## What remains local-only

`make infra-check` formats and validates Terraform and executes provider-mocked plan assertions. Those assertions guard the chosen instance type, standard CPU credits, volume size and deletion, IMDSv2, ingress restriction, and removable ECR repository without contacting AWS.

`make rehost-smoke` exercises the complete runtime locally in an isolated Compose project. It verifies migrations, readiness, non-root processes, ingestion, downstream delivery, and durable PostgreSQL state across a restart, then removes its containers, network, and volume even on failure.

The frozen-workload controller is also prepared locally. `make aws-rehost-workload` runs the exact Step 8.6 healthy workload rates from the approved developer machine, not from the small EC2 host. For every rate it generates the manifest locally, uses SSM to regenerate and prepare the same run inside the private Compose network, drives the public API with local k6, samples API runtime metrics, then asks the private helper to reconcile PostgreSQL and simulator evidence. PostgreSQL and the simulator never receive public ports.

The ignored session bundle records the workload contract, driver placement, region, instance type, process counts, database placement, manifests, k6 summaries, runtime samples, compact reconciliation, and per-rate pass interpretation. It deliberately omits the temporary API address, instance ID, AWS account ID, and ECR repository URL from the workload result files. This is migration and benchmark-portability evidence, not the final fixed-versus-elastic causal comparison.

## Stage 9.2 private data layer

Terraform now defines the RDS boundary without provisioning it. The database is RDS for PostgreSQL 17 on a single-AZ `db.t4g.micro` instance with 20 GiB of encrypted gp3 storage. A saved plan resolves the major version to a concrete available minor and verifies that the engine, class, and storage combination is orderable in Jakarta. Automatic minor upgrades, storage autoscaling, Multi-AZ, Performance Insights, enhanced monitoring, retained automated backups, final snapshots, and deletion protection are disabled for this short synthetic experiment.

RDS requires its subnet group to cover two Availability Zones even for this single-AZ instance. The module therefore adds two private subnets with a route table containing no internet or NAT route. The database receives no public address; its security group accepts port 5432 only from the rehost security group. Placing the instance in the host's Availability Zone avoids deliberate cross-AZ traffic during this migration checkpoint.

RDS generates the master password and manages it in Secrets Manager, keeping plaintext credentials out of configuration and Terraform state. The EC2 role can retrieve only that specific managed secret. AWS documents that deleting a database with an RDS-managed credential also deletes the secret. Native teardown verification nevertheless checks the instance, subnet and parameter groups, manual snapshots, retained automated backups, and matching RDS-managed secrets—including secrets pending deletion.

The guarded RDS deployment is available as `make aws-rds-deploy`. Its installer can start directly on a fresh host and no longer depends on a preceding host-local workload or `.env` file. The SSM payload contains the deterministic database identifier and pinned image inputs, but no endpoint, secret ARN, or password. On EC2, the instance role discovers RDS metadata, retrieves its one allowed secret, URL-encodes the credential, creates a TLS-required mode-`0600` runtime file, migrates RDS, starts the API and simulator, then proves ingestion and persistence across an API restart. When invoked over an earlier rehost it also removes the obsolete host-local containers without deleting their volume. Only the resolved PostgreSQL minor and non-secret capacity configuration enter session evidence.

The guarded `make aws-rds-correctness` checkpoint now reuses the shared normal, duplicate, out-of-order, and downstream-outage scenario implementation. A one-off application container drives the API and simulator through private Compose names and reconciles directly against RDS. Only a base64-framed compact report returns through SSM: suite and test-run identities, HTTP status codes, and reconciliation counts. It contains no database endpoint, credential, AWS account ID, or public API address. A real AWS plan still waits for an authenticated CLI session and the account owner's approval of cloud session 1's exact resource list, estimate, duration, and cost ceiling.

## Image publication and deployment controller

`make aws-rehost-publish` is available only after the guarded Terraform apply. It requires the clean Git revision recorded by the approved plan, builds `linux/amd64`, pushes a Git-revision tag to the session ECR repository, reads the registry digest back, logs Docker out, and saves only the architecture, tag, and digest in session evidence. The ECR password is passed through standard input and is never placed in arguments, output, or evidence. Deployment subsequently uses the immutable digest, not the mutable tag.

`make aws-rehost-deploy` waits for the specific instance's SSM agent, sends the committed Compose and installer files through `AWS-RunShellScript`, waits for success, and records only the command ID and outcome. It never opens SSH. AWS warns that Run Command parameters are retained in command history and can be recorded by CloudTrail, so the payload contains no password. The installer generates the synthetic PostgreSQL password on the instance and stores its env file with mode `0600`.

The bootstrap installs Docker Compose v2.32.4 for AMD64 from Docker's official release and verifies its published SHA-256 checksum before installation. The remote command waits for cloud-init, authenticates to ECR with the instance role, starts the digest-pinned application, verifies migration head and readiness, and sends one unique Courier Alpha event through the database and simulator. Cloud session 1 exercised the same workflow with its earlier ARM64 build; the current x86_64 revision is locally verified and awaits its next cloud run.

`make aws-rehost-workload` has the same revision guard and additionally requires a successfully deployed rehost. Its SSM payload contains only synthetic workload parameters and paths to the host-private Compose configuration. The external driver learns the temporary API address in memory from Terraform output, but no workload result artifact persists it. A k6 threshold failure is still reconciled so that a failing point can be interpreted; infrastructure teardown remains a separate unconditional command.

Every cloud session remains disposable and subject to explicit approval and verified teardown.

## Chargeable footprint when eventually applied

Cloud session 1 used one ARM64 `t4g.small` instance, its encrypted 16 GiB gp3 root volume, one public IPv4 while the instance ran, ECR image storage, one single-AZ `db.t4g.micro` PostgreSQL 17.11 instance, fixed encrypted 20 GiB gp3 database storage, and one RDS-managed Secrets Manager secret. There was no NAT gateway, load balancer, Multi-AZ standby, retained database backup, or final snapshot. It was approved with a USD 2.00 ceiling after an AWS Price List estimate of USD 0.3233 for the three-hour maximum.

The frozen synchronous workload established 25 events/s as the maximum sustainable rate and 50 events/s as the first failing rate on this rehost. The later RDS checkpoint passed normal, duplicate, out-of-order, and downstream-outage reconciliation. This is portability and migration evidence, not the later fixed-versus-elastic headline comparison. Terraform destroy completed, Terraform state was empty, and every native resource inventory—including the RDS-managed secret—returned zero.

Teardown is not considered complete merely because Terraform destroy succeeds. `make aws-verify-down` requires empty Terraform state and zero native EC2, EBS, network, all-service ECR, SQS, IAM, RDS, snapshot, retained-backup, and RDS-managed-secret inventories. It also saves the generic tag-index count, but does not mistake its previously tagged resource tombstones for live resources. Later AWS resource types must extend the native verifier before they are used.

## References

- [Amazon EC2 T4g availability in Asia Pacific (Jakarta)](https://aws.amazon.com/about-aws/whats-new/2023/06/amazon-ec2-t4g-instances-additional-regions/)
- [AWS Systems Manager Agent on Amazon Machine Images](https://docs.aws.amazon.com/systems-manager/latest/userguide/ami-preinstalled-agent.html)
- [Terraform provider mocking and tests](https://developer.hashicorp.com/terraform/language/tests/mocking)
- [AWS Systems Manager Run Command security guidance](https://docs.aws.amazon.com/systems-manager/latest/userguide/running-commands.html)
- [Docker Compose plugin installation on Linux](https://docs.docker.com/compose/install/linux/)
- [RDS DB subnet-group requirements](https://docs.aws.amazon.com/AmazonRDS/latest/APIReference/API_CreateDBSubnetGroup.html)
- [RDS-managed master credentials](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/rds-secrets-manager.html)
- [RDS PostgreSQL instance-class support](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Concepts.DBInstanceClass.Support.html)
