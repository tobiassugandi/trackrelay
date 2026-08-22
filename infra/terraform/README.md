# TrackRelay AWS infrastructure

This directory is the Terraform root module for TrackRelay's disposable AWS experiment environments. The initial foundation configures only Terraform and the AWS provider: it declares **no AWS resources**.

Initialize the pinned provider locally:

```bash
make infra-init
```

Check formatting and validate the configuration:

```bash
make infra-check
```

These commands do not provision infrastructure. Do not run `terraform apply` directly; use the guarded lifecycle commands below and follow the cloud-session checklist.

The lifecycle commands are now available but must not be used to provision without the checklist's explicit approval:

```bash
make aws-plan SESSION_ID=cloud-session-1-20260822T090000Z
make aws-up \
  SESSION_ID=cloud-session-1-20260822T090000Z \
  APPROVED_SESSION_ID=cloud-session-1-20260822T090000Z \
  APPROVED_COST_CEILING_USD=5
make aws-down SESSION_ID=cloud-session-1-20260822T090000Z
make aws-verify-down SESSION_ID=cloud-session-1-20260822T090000Z
```

`aws-plan` saves an immutable plan hash and non-secret session record. `aws-up` refuses a mismatched session, a changed plan or Git revision, and a cost ceiling above the monthly budget. `aws-down` intentionally has no approval gate so recovery cannot block teardown. `aws-verify-down` currently supplies the generic state and tag checks; native service checks are added alongside future resources.

The provider reads credentials from the private AWS CLI profile selected by Terraform input. Credentials and local `*.tfvars` files must not be committed. Terraform state can contain sensitive values and is also excluded from Git.

Every future resource inherits `Project=TrackRelay`, `Environment=experiment`, `ManagedBy=terraform`, and a unique `SessionId` tag. The session ID connects the Terraform state, AWS-side inventory, experiment evidence, and teardown proof for one bounded cloud session. See the [cloud-session checklist](../../docs/aws-cloud-session-checklist.md) for the required lifecycle.
