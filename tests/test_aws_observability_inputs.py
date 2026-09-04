"""One strict output schema supports historical and high-resolution deployments."""

from pytest import mark

from tests.test_aws_elasticity_cloudwatch import DIMENSIONS
from trackrelay.aws_elasticity_cloudwatch import build_elasticity_metric_queries
from trackrelay.aws_observability_inputs import valid_async_observability_dimensions


def test_optional_scaling_namespace_preserves_native_queries():
    current = {**DIMENSIONS, "scaling_metrics_namespace": "TrackRelay/Elasticity"}
    assert valid_async_observability_dimensions(DIMENSIONS)
    assert valid_async_observability_dimensions(current)
    assert build_elasticity_metric_queries(current) == build_elasticity_metric_queries(
        DIMENSIONS
    )


@mark.parametrize(
    "value",
    [
        None,
        [],
        {},
        {**DIMENSIONS, "extra": "unexpected"},
        {**DIMENSIONS, "scaling_metrics_namespace": "wrong"},
        {**DIMENSIONS, "scaling_metrics_namespace": None},
        {**DIMENSIONS, "cluster_name": ""},
        {**DIMENSIONS, "cluster_name": 123},
    ],
)
def test_invalid_or_unknown_dimensions_are_rejected(value):
    assert not valid_async_observability_dimensions(value)


@mark.parametrize("key", sorted(DIMENSIONS))
def test_every_native_identity_is_still_required(key):
    current = {**DIMENSIONS, "scaling_metrics_namespace": "TrackRelay/Elasticity"}
    del current[key]
    assert not valid_async_observability_dimensions(current)
