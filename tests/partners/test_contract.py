"""Regression tests for the reusable adapter contract suite itself."""

from datetime import datetime

from pydantic import BaseModel
from pytest import raises

from trackrelay.domain import NormalizedEvent, ShipmentStatus

from .contract import PartnerAdapterContract


class ExpectedPayload(BaseModel):
    """Payload model independently expected by the concrete contract test."""

    status: str


class IncorrectlyAdvertisedPayload(BaseModel):
    """Different model accidentally advertised by an adapter."""

    status: str


class IncorrectPayloadModelAdapter:
    """Protocol-shaped adapter with the wrong payload-model declaration."""

    adapter_type = "incorrect-payload-model"
    payload_model = IncorrectlyAdvertisedPayload

    def normalize(
        self,
        payload: ExpectedPayload,
        *,
        partner_id: str,
        received_at: datetime,
    ) -> NormalizedEvent:
        """Remain unused because declaration validation must fail first."""
        raise AssertionError("normalization should not run")


class IncorrectPayloadModelContract(PartnerAdapterContract[ExpectedPayload]):
    """Concrete contract fixture containing an intentional declaration bug."""

    adapter = IncorrectPayloadModelAdapter()
    expected_adapter_type = "incorrect-payload-model"
    expected_payload_model = ExpectedPayload

    def make_payload(self, status: ShipmentStatus) -> ExpectedPayload:
        return self.expected_payload_model(status=status.value)


def test_contract_rejects_an_incorrectly_advertised_payload_model() -> None:
    contract = IncorrectPayloadModelContract()

    with raises(AssertionError):
        contract.test_adapter_implements_the_shared_protocol()
