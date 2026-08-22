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

These commands do not provision infrastructure. Do not run `terraform apply` directly. Explicit, guarded provision and destroy commands will be added with the cloud-session checklist before the first AWS session.

The provider reads credentials from the private AWS CLI profile selected by Terraform input. Credentials and local `*.tfvars` files must not be committed. Terraform state can contain sensitive values and is also excluded from Git.
