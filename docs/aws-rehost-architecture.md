# Stage 9.1 synchronous rehost architecture

Stage 9.1 is a portability checkpoint for the unchanged synchronous TrackRelay application. It is not the final modern architecture and it is not the fixed-versus-elastic causal experiment.

```text
approved benchmark host
        |
        | TCP 8000 from one IPv4 /32
        v
public subnet: one t4g.small EC2 instance
        |- TrackRelay API container       (next slice)
        |- PostgreSQL container           (next slice)
        `- downstream simulator container (next slice)

EC2 pulls the API image from ECR and is administered through SSM, not SSH.
```

## What Terraform defines now

The module defines one dedicated VPC, one public subnet and route, an internet gateway, a restricted security group, one ECR repository, the minimum EC2 IAM role and instance profile, and one ARM Amazon Linux 2023 `t4g.small` host. The instance uses standard CPU credits and one encrypted 16 GiB gp3 root volume that is deleted on termination.

The module deliberately has no NAT gateway, load balancer, Elastic IP, SSH key, snapshot, or RDS instance. The instance receives a temporary public IPv4 so it can reach ECR and SSM without a chargeable NAT gateway. Only port `8000` is reachable, and only from the benchmark location's explicitly supplied `/32`; port `22` is closed. IMDSv2 tokens are required.

Amazon EC2 T4g is available in Asia Pacific (Jakarta), and the selected Amazon Linux 2023 ARM image supports the architecture. AWS documents that the SSM Agent is commonly preinstalled on Amazon Linux 2023 images; bootstrap also enables it explicitly.

## What remains local-only

`make infra-check` formats and validates Terraform and executes provider-mocked plan assertions. Those assertions guard the chosen instance type, standard CPU credits, volume size and deletion, IMDSv2, ingress restriction, and removable ECR repository without contacting AWS.

The next Stage 9.1 slice will define the three-container host runtime. Stage 9.2 will replace host-local PostgreSQL with RDS for the target AWS deployment. A real AWS plan waits until both stages are locally prepared, the private CLI session is authenticated, and the account owner approves cloud session 1's exact resource list, estimate, duration, and cost ceiling.

No AWS resources were created while preparing this architecture.

## Chargeable footprint when eventually applied

The planned Stage 9.1-only footprint is one `t4g.small` instance, 16 GiB of gp3 storage, one public IPv4 while the instance runs, and ECR image storage. The VPC components, IAM objects, and security group are not themselves the intended billable capacity. Stage 9.2 will add RDS and will require a revised resource list and estimate before approval.

Teardown is not considered complete merely because Terraform destroy succeeds. `make aws-verify-down` also checks empty session tags and native EC2, EBS, network, ECR, and IAM inventories. Later AWS resource types must extend that verifier before they are used.

## References

- [Amazon EC2 T4g availability in Asia Pacific (Jakarta)](https://aws.amazon.com/about-aws/whats-new/2023/06/amazon-ec2-t4g-instances-additional-regions/)
- [AWS Systems Manager Agent on Amazon Machine Images](https://docs.aws.amazon.com/systems-manager/latest/userguide/ami-preinstalled-agent.html)
- [Terraform provider mocking and tests](https://developer.hashicorp.com/terraform/language/tests/mocking)
