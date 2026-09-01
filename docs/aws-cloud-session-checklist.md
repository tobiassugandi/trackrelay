# AWS cloud-session checklist

TrackRelay uses AWS only for four bounded validation or experiment sessions. This checklist is the contract for starting, operating, and closing each session. It applies even when a run fails or is interrupted.

## Ownership and approval gate

Codex prepares the infrastructure, commands, estimate, validation, evidence collection, and teardown. The human account owner privately completes authentication and explicitly approves the billable session.

Before requesting approval, Codex must present:

- the session number, purpose, and exact stages being validated;
- the Git commit and Terraform provider lock being deployed;
- the region and benchmark-driver location;
- every intended AWS service, resource type, quantity, and size;
- the estimated duration, estimated cost, and proposed session cost ceiling;
- the current monthly budget context and enough margin beneath the USD 25 limit;
- the provision, evidence-collection, destroy, and inventory commands; and
- the recovery procedure if the controlling process or network connection fails.

Approval must identify the session and its cost ceiling. A generic `continue` does not authorize AWS charges. Any later change that increases resources, duration, scope, or the ceiling requires fresh approval.

## Session identity and evidence

Create one ID before planning infrastructure:

```text
cloud-session-<1|2|3|4>-<UTC timestamp>

example: cloud-session-1-20260822T090000Z
```

Every Terraform-managed resource must inherit these tags:

```text
Project=TrackRelay
Environment=experiment
ManagedBy=terraform
SessionId=<session ID>
```

Store the compact, non-secret session record under `results/aws-sessions/<session ID>/`. It must contain the approved proposal, mutually exclusive `rehost` or `async` deployment mode, configuration and Git revision, saved plans, command logs, pre-destroy inventory, post-destroy inventory, experiment or validation evidence, and final outcome. Do not record credentials, account IDs, private endpoints, secret values, or complete Terraform state.

## Before provisioning

- [ ] Confirm the repository is at the intended clean Git revision.
- [ ] Run `make aws-check` and confirm the non-root profile and Jakarta region.
- [ ] Run `make infra-init` and `make infra-check`.
- [ ] Confirm that Terraform state will survive a terminal or process interruption; do not provision from disposable local state.
- [ ] Identify the approved benchmark driver's public IPv4 and express it as one `/32`; do not authorize a broad ingress range.
- [ ] Generate and save the exact Terraform plan with `make aws-plan SESSION_ID=<session ID> AWS_DEPLOYMENT_MODE=<rehost|async> API_INGRESS_CIDR=<approved IPv4>/32`; this does not apply it. Confirm the plan excludes the other runtime topology.
- [ ] Review the plan's add/change/destroy counts and reconcile every planned object with the approved resource list.
- [ ] Confirm that the session ID is new and appears in the provider's default tags.
- [ ] Confirm budget headroom and obtain explicit human approval for this plan and cost ceiling.

## While AWS is on

- [ ] Record the UTC start time and start the session-duration timer.
- [ ] Apply only the saved, approved plan with `make aws-up SESSION_ID=<session ID> AWS_DEPLOYMENT_MODE=<same saved mode> API_INGRESS_CIDR=<approved IPv4>/32 APPROVED_SESSION_ID=<same session ID> APPROVED_COST_CEILING_USD=<approved ceiling>`.
- [ ] For a historical Stage 9.1/9.2 session only, run the separately approved publication, rehost deployment, portability workload, RDS deployment, and RDS correctness commands. Do not carry that host-local workload into Stage 9.3.
- [ ] For a dedicated sampler-canary proposal only, arm `make aws-scaling-canary` by repeating the approved session ID, recorded cost ceiling, and unconditional-teardown session ID. Let it own the single 10 events/s × 30-second point, destroy, and native verification; do not also start the full scaling runner.
- [ ] For cloud session 2 only, invoke `make aws-scaling-session` immediately after the approved apply by repeating the approved session ID, recorded cost ceiling, exact `t3.small,c7i-flex.large` tier order, and unconditional-teardown session ID. Let it own image publication, direct RDS deployment, correctness, clean-state and CPU-credit gates, both short treatments, the transition, reporting, destroy, and native absence verification; do not run a competing controller.
- [ ] For cloud session 3 only, invoke `make aws-async-deploy` immediately after the approved async foundation apply by repeating the approved session ID, cost ceiling, and unconditional-teardown session ID. Let it own all three image publications, both exact Terraform phase plans, the standalone migration, and fixed-service convergence. It tears down and verifies after failure or interruption; after success, continue directly into the approved integration runner and do not manually alter the stack.
- [ ] For cloud session 3 only, immediately follow successful convergence with `make aws-async-integration` using the same repeated approval values. Let it own the exact 1-, 10-, and 100-event total batches (counts, not events/s), continuous request and drain evidence, reconciliation, representative CloudWatch evidence, complete destroy, and native absence verification. Do not run a second workload or teardown controller in parallel.
- [ ] Do not create untracked resources in the AWS console. If emergency diagnosis creates or changes anything, record it immediately and bring it under Terraform or remove it before continuing.
- [ ] Run only the validation or experiment named in the approved proposal.
- [ ] Collect evidence continuously so an interrupted run can still be diagnosed.
- [ ] If validation fails, costs approach the ceiling, or the session exceeds its approved duration, stop experimentation and begin teardown.
- [ ] A normal experiment error, `SIGINT`, or `SIGTERM` should be allowed to reach the runner's cleanup path. If the local process or host disappears abruptly, recover `deployment_mode` and `rehost_instance_type` from the session manifest and manually run `make aws-down` followed by `make aws-verify-down` with those exact values.

## Teardown

- [ ] Confirm workload generation has stopped and the evidence required by the approved workflow is readable before teardown. Stage 9.3 requires its comparison report and internal correctness evidence, not a host-local `rehost-workload` result.
- [ ] Save `terraform state list` and an AWS inventory filtered by both `Project=TrackRelay` and the session ID.
- [ ] Generate and review a destroy plan covering every object in the pre-destroy Terraform state.
- [ ] Run `make aws-down SESSION_ID=<session ID> AWS_DEPLOYMENT_MODE=<same saved mode> API_INGRESS_CIDR=<approved IPv4>/32` to generate and apply the complete destroy plan. This command deliberately has no approval gate; do not rely on targeted destroy for normal teardown.
- [ ] Wait for asynchronous deletions to reach their terminal deleted state.
- [ ] Save the empty post-destroy `terraform state list`.
- [ ] Run `make aws-verify-down SESSION_ID=<session ID> AWS_DEPLOYMENT_MODE=<same saved mode> API_INGRESS_CIDR=<approved IPv4>/32` to prove empty Terraform state and query both AWS's Resource Groups Tagging API and the native service inventories.
- [ ] Use the pre-destroy inventory to run native, service-specific absence checks. The generic tagging API is supporting evidence, not absence proof: AWS documents that `GetResources` returns tagged **or previously tagged** resources, so deleted-resource tombstones can remain after every native inventory is empty.
- [ ] Record the UTC finish time, observed duration, outcome, and any deviation from the approved plan.

## Required service-specific absence checks

The verifier must cover every service used by the session. At minimum, when present in the pre-destroy inventory, confirm the absence of:

- EC2 instances, Elastic IP allocations, NAT gateways, and session-owned network interfaces;
- Application Load Balancers, listeners, and target groups;
- ECS services, running or pending tasks, and session-owned clusters;
- RDS instances or clusters, manual snapshots, and retained automated backups;
- ECR repositories and stored images;
- SQS queues and dead-letter queues;
- CloudWatch log groups, alarms, and dashboards;
- Secrets Manager secrets, including secrets pending deletion; and
- any additional resource type introduced by the approved Terraform plan.

VPCs, subnets, route tables, security groups, and related non-billable dependencies must also be removed so they cannot conceal or block deletion of a chargeable resource.

For synthetic TrackRelay data, an RDS instance must be deleted rather than merely stopped. AWS documents that stopped RDS instances retain chargeable storage and automatically restart after seven days.

## Definition of a closed session

A session is closed only when all of the following are true:

1. The destroy command succeeded.
2. Terraform state contains no managed resources.
3. The post-destroy generic tagging-index count is saved and interpreted as a potentially historical record, never as proof that a returned resource still exists.
4. Every service-specific native absence check is empty.
5. The proposal, evidence, destroy log, and both inventories are saved locally.

A successful Terraform destroy by itself is not sufficient. If any check fails or cannot run, the session remains open and teardown work continues. Billing dashboards and budget alerts may be reviewed later, but their delayed data is not the immediate teardown proof.

The current `aws-verify-down` command saves the generic tag-index count and implements authoritative Terraform-state plus native EC2, EBS, network, ELBv2 load-balancer and target-group, ECS cluster, pending/running task, service and active-task-definition, Cloud Map namespace, CloudWatch Logs and exact session-dashboard, all-service ECR, SQS, all defined IAM roles, RDS instance, RDS subnet-group, RDS parameter-group, manual-snapshot, retained-automated-backup, and RDS-managed-secret checks through the Stage 9.5 runtime definitions. Before a later increment introduces another resource type, its native service-specific absence check must be added to the verifier and tested.

## References

- [Terraform destroy command](https://developer.hashicorp.com/terraform/cli/commands/destroy)
- [AWS Resource Groups Tagging API `GetResources`](https://docs.aws.amazon.com/resourcegroupstagging/latest/APIReference/API_GetResources.html)
- [Stopping an Amazon RDS DB instance temporarily](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_StopInstance.html)
