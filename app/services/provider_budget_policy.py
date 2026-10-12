"""Pure admission policy; vendor billing and tokenization remain authoritative."""

import math
from dataclasses import dataclass

from app.core.config import Settings

BACKGROUND_OPERATIONS = frozenset({"graph_extraction", "document_embedding"})
OUTPUT_ALLOWANCES = {"graph_extraction": 2_000, "answer_synthesis": 1_200, "exam_generation": 2_500}


@dataclass(frozen=True)
class BudgetUsage:
    minute_requests: int = 0
    day_requests: int = 0
    minute_estimated_tokens: int = 0
    day_estimated_tokens: int = 0


def estimated_reservation(input_characters: int, operation: str) -> int:
    if type(input_characters) is not int or input_characters < 0:
        raise ValueError("Provider input character count must be a nonnegative integer.")
    # The output allowance is retained even on errors/cancellation; it is not
    # a measured completion length or a universal hard provider output cap.
    return (input_characters + 3) // 4 + OUTPUT_ALLOWANCES.get(operation, 0)


def admission_denial(config: Settings, usage: BudgetUsage, *, operation: str,
                     estimated_tokens: int, epoch: float) -> tuple[str, float] | None:
    background = operation in BACKGROUND_OPERATIONS
    denials = []
    for period, seconds in (("minute", 60), ("day", 86_400)):
        retry = max(1.0, (math.floor(epoch / seconds) + 1) * seconds - epoch)
        request_limit = getattr(config, f"provider_requests_per_{period}")
        request_reserve = getattr(config, f"provider_interactive_request_reserve_per_{period}") if background else 0
        if getattr(usage, f"{period}_requests") + 1 > request_limit - request_reserve:
            denials.append((f"{period} request", retry))
        token_limit = getattr(config, f"provider_estimated_tokens_per_{period}")
        token_reserve = getattr(config, f"provider_interactive_token_reserve_per_{period}") if background else 0
        if token_limit and getattr(usage, f"{period}_estimated_tokens") + estimated_tokens > token_limit - token_reserve:
            denials.append((f"{period} estimated-token", retry))
    # If several windows are full, waiting for only the shortest reset would
    # cause another avoidable retry. UTC database time determines every window.
    return max(denials, key=lambda item: item[1]) if denials else None
