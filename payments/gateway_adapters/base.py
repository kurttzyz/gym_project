"""Contracts for future providers. No external charges are implemented here."""
from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping, Protocol
from uuid import UUID


@dataclass(frozen=True)
class PaymentRequest:
    gym_key: UUID
    invoice_id: str
    amount: Decimal
    currency: str
    idempotency_key: str


@dataclass(frozen=True)
class CheckoutSession:
    provider_reference: str
    checkout_url: str


@dataclass(frozen=True)
class VerifiedPaymentEvent:
    event_id: str
    provider_reference: str
    status: str
    amount: Decimal
    currency: str


class PaymentGateway(Protocol):
    def create_checkout(self, request: PaymentRequest) -> CheckoutSession: ...

    def verify_webhook(self, body: bytes, headers: Mapping[str, str]) -> VerifiedPaymentEvent: ...
