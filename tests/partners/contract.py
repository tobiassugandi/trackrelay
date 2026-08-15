"""Reusable behavioral contract for every courier adapter."""

from abc import ABC, abstractmethod
from datetime import UTC, datetime

from pydantic import BaseModel
from pytest import mark

from trackrelay.domain import ShipmentStatus
from trackrelay.partners import PartnerAdapter


class PartnerAdapterContract[PayloadT: BaseModel](ABC):
    """Tests inherited by each concrete courier adapter test class."""

    adapter: PartnerAdapter[PayloadT]
    expected_partner_id: str
    expected_partner_event_id: str
    expected_tracking_number: str
    expected_occurred_at: datetime
    received_at = datetime(2026, 8, 15, 4, 0, tzinfo=UTC)

    @abstractmethod
    def make_payload(self, status: ShipmentStatus) -> PayloadT:
        """Build one valid courier payload for a normalized status."""

    def test_adapter_implements_the_shared_protocol(self) -> None:
        payload = self.make_payload(ShipmentStatus.CREATED)

        assert isinstance(self.adapter, PartnerAdapter)
        assert self.adapter.partner_id == self.expected_partner_id
        assert isinstance(payload, self.adapter.payload_model)

    @mark.parametrize("normalized_status", list(ShipmentStatus))
    def test_adapter_normalizes_the_shared_event_contract(
        self,
        normalized_status: ShipmentStatus,
    ) -> None:
        payload = self.make_payload(normalized_status)

        event = self.adapter.normalize(payload, received_at=self.received_at)

        assert event.partner_id == self.expected_partner_id
        assert event.partner_event_id == self.expected_partner_event_id
        assert event.tracking_number == self.expected_tracking_number
        assert event.status is normalized_status
        assert event.occurred_at == self.expected_occurred_at
        assert event.received_at == self.received_at
        assert event.raw_payload == payload.model_dump(mode="json", by_alias=True)
