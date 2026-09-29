from typing import Literal
from pydantic import BaseModel, Field


class TicketCreate(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    issue: str = Field(min_length=3, max_length=4000)


class RefundProposalCreate(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    order_id: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=2, max_length=2000)


class RefundReview(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    order_id: str = Field(min_length=1, max_length=64)
    decision: Literal['approve', 'reject']
    review_comment: str = Field(default='', max_length=2000)


class RefundExecute(BaseModel):
    """Execute the proposal named here — not "the newest approved one".

    ``proposal_id`` is required so the caller has to state which approval it is
    consuming. Without it the service had to guess, and a guess that happens to
    be right is indistinguishable in the audit trail from a guess that is wrong.
    """

    user_id: str = Field(min_length=1, max_length=64)
    order_id: str = Field(min_length=1, max_length=64)
    proposal_id: str = Field(min_length=1, max_length=64)


class AftersaleEligibilityQuery(BaseModel):
    """Ask whether an order may be returned. A read, but a POST.

    It is a query with no side effect, so GET would be the obvious verb — except
    the answer depends on who is asking (ownership is checked), and a GET with a
    user id in the query string would put that identity in every access log.
    """

    user_id: str = Field(min_length=1, max_length=64)
    order_id: str = Field(min_length=1, max_length=64)
    rule_type: str = Field(default='return_no_reason', max_length=32)
