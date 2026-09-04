"""Shared Terraform observability output contract for every async controller."""

from typing import TypeGuard

NATIVE_DIMENSION_KEYS = frozenset(
    {
        "api_service_name",
        "cluster_name",
        "dashboard_name",
        "dead_letter_queue_name",
        "delivery_queue_name",
        "load_balancer_dimension",
        "rds_identifier",
        "simulator_service_name",
        "worker_service_name",
    }
)
SCALING_METRICS_NAMESPACE = "TrackRelay/Elasticity"


def valid_async_observability_dimensions(value: object) -> TypeGuard[dict[str, str]]:
    """Accept native-only history or the exact high-resolution extension.

    Do not accept arbitrary extra fields, empty identities, or a namespace that
    differs from the publisher/IAM/alarm contract. Resource-specific identity
    checks remain the responsibility of the consuming controller.
    """
    return (
        isinstance(value, dict)
        and set(value) - {"scaling_metrics_namespace"} == NATIVE_DIMENSION_KEYS
        and all(isinstance(item, str) and item for item in value.values())
        and value.get("scaling_metrics_namespace", SCALING_METRICS_NAMESPACE)
        == SCALING_METRICS_NAMESPACE
    )
