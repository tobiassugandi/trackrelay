# TrackRelay AWS infrastructure

This directory is the Terraform root module for TrackRelay's disposable AWS experiment environments. It currently defines the minimal Stage 9.1 synchronous-rehost host and its supporting resources. It has not been applied to AWS.

The current module contains:

- one `t4g.small` EC2 instance using the current ARM Amazon Linux 2023 AMI;
- one encrypted 16 GiB gp3 root volume deleted with the instance;
- one dedicated VPC and public subnet with an internet gateway, but no NAT gateway;
- one security group exposing only API port `8000` to an explicitly approved IPv4 `/32`, with no SSH ingress;
- one ECR repository that can be emptied during teardown; and
- an instance role for ECR reads and Systems Manager access.

The API, one-off migration, host-local PostgreSQL, and downstream simulator runtime is defined separately in `deploy/rehost/compose.yaml`. Deployment automation is the next Stage 9.1 slice. RDS is a separate Stage 9.2 concern. See the [Stage 9.1 architecture note](../../docs/aws-rehost-architecture.md).

Initialize the pinned provider locally:

```bash
make infra-init
```

Check formatting, validate the configuration, and run mocked Terraform plan assertions:

```bash
make infra-check
```

These commands do not provision infrastructure. Do not run `terraform apply` directly; use the guarded lifecycle commands below and follow the cloud-session checklist.

The lifecycle commands are now available but must not be used to provision without the checklist's explicit approval:

```bash
make aws-plan \
  SESSION_ID=cloud-session-1-20260822T090000Z \
  API_INGRESS_CIDR=203.0.113.10/32
make aws-up \
  SESSION_ID=cloud-session-1-20260822T090000Z \
  API_INGRESS_CIDR=203.0.113.10/32 \
  APPROVED_SESSION_ID=cloud-session-1-20260822T090000Z \
  APPROVED_COST_CEILING_USD=5
make aws-down \
  SESSION_ID=cloud-session-1-20260822T090000Z \
  API_INGRESS_CIDR=203.0.113.10/32
make aws-verify-down \
  SESSION_ID=cloud-session-1-20260822T090000Z \
  API_INGRESS_CIDR=203.0.113.10/32
```

Replace the documentation-only address with the public IPv4 `/32` of the approved benchmark location. The Makefile's `127.0.0.1/32` default is deliberately safe: a forgotten override produces an unreachable cloud API rather than public ingress.

`aws-plan` saves an immutable plan hash and non-secret session record, including the approved ingress CIDR. `aws-up` refuses a mismatched session, a changed plan or Git revision, and a cost ceiling above the monthly budget. `aws-down` intentionally has no approval gate so recovery cannot block teardown. `aws-verify-down` supplies the generic state and tag checks plus native checks for every resource type currently introduced by this module.

After an approved `aws-up`, `make aws-rehost-publish` builds and pushes only Linux ARM64 and records its immutable digest. `make aws-rehost-deploy` transfers the runtime through SSM, generates the synthetic database password on the host, starts the stack, and runs a tiny smoke event. Both commands require the same clean Git revision as the applied plan. They are stateful cloud-session commands, not local validation commands, and must not be run merely because their implementation exists.

The provider reads credentials from the private AWS CLI profile selected by Terraform input. Credentials and local `*.tfvars` files must not be committed. Terraform state can contain sensitive values and is also excluded from Git.

Every future resource inherits `Project=TrackRelay`, `Environment=experiment`, `ManagedBy=terraform`, and a unique `SessionId` tag. The session ID connects the Terraform state, AWS-side inventory, experiment evidence, and teardown proof for one bounded cloud session. See the [cloud-session checklist](../../docs/aws-cloud-session-checklist.md) for the required lifecycle.
