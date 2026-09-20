"""Fixed Northstar company policy.

Learned procedures may guide investigation. They cannot edit this policy,
raise its fee limit, or authorize a financial treatment.
"""

from __future__ import annotations

from precedent.models import CompanyPolicy, PaymentChannel, hash_model

POLICY_ID = "northstar-usd-v1"
COMPANY_ID = "NORTHSTAR"
ALLOWED_CASH_ACCOUNT_ID = "CASH-US-01"
SUPPORTED_FEE_TYPE = "RECEIVING_WIRE_FEE"
MAX_RECEIVING_WIRE_FEE_CENTS = 5000

NORTHSTAR_POLICY = CompanyPolicy(
    policy_id=POLICY_ID,
    company_id=COMPANY_ID,
    allowed_cash_account_id=ALLOWED_CASH_ACCOUNT_ID,
    currency="USD",
    supported_channels=[PaymentChannel.WIRE, PaymentChannel.ACH],
    allow_receiving_wire_fee=True,
    max_receiving_wire_fee_cents=MAX_RECEIVING_WIRE_FEE_CENTS,
    max_bundle_invoices=3,
    allow_partial_settlement=False,
    allow_writeoff=False,
    require_remittance=True,
)


def northstar_policy() -> CompanyPolicy:
    """Return the canonical Northstar USD v1 policy."""
    return NORTHSTAR_POLICY


def policy_hash(policy: CompanyPolicy | None = None) -> str:
    """SHA-256 of the canonical JSON policy payload."""
    return hash_model(NORTHSTAR_POLICY if policy is None else policy)


__all__ = [
    "ALLOWED_CASH_ACCOUNT_ID",
    "COMPANY_ID",
    "MAX_RECEIVING_WIRE_FEE_CENTS",
    "NORTHSTAR_POLICY",
    "POLICY_ID",
    "SUPPORTED_FEE_TYPE",
    "northstar_policy",
    "policy_hash",
]
