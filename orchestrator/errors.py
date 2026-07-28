from __future__ import annotations

from .models import Diagnostic, Receipt


class ReceiptFailure(RuntimeError):
    """Preserve an adapter's typed failure while crossing a graph node boundary."""

    def __init__(self, receipt: Receipt):
        if receipt.success or receipt.failure is None:
            raise ValueError("ReceiptFailure requires an unsuccessful receipt")
        self.receipt = receipt
        super().__init__(receipt.failure.summary)


class DiagnosticFailure(RuntimeError):
    """Raise an expected typed diagnostic without flattening it to policy text."""

    def __init__(self, diagnostic: Diagnostic):
        self.diagnostic = diagnostic
        super().__init__(diagnostic.summary)
