class TradeRefused(Exception):
    """``A trade was refused. ``reason`` is machine-readable."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class RulesIncomplete(TradeRefused):
    def __init__(self, missing: list[str]) -> None:
        super().__init__("rules_incomplete", ",".join(missing))
        self.missing = missing


class LiveDisabled(TradeRefused):
    def __init__(self, detail: str = "") -> None:
        super().__init__("live_disabled", detail)


class DrawdownReview(TradeRefused):
    def __init__(self) -> None:
        super().__init__("drawdown_review", "post /paper/review/ack to continue")
