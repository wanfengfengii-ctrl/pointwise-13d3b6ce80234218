from __future__ import annotations

from typing import TYPE_CHECKING

from control.receipts import CorrectionReceipt
from editions.revisions import check_compatibility
from errors import conflict

if TYPE_CHECKING:
    from control.runtime import Runtime


def _observe(runtime: Runtime, receipt: CorrectionReceipt) -> CorrectionReceipt:
    if receipt.status == 'confirmed':
        return receipt
    outcome = runtime.screen.find_cutover(receipt.show, receipt.screen, receipt.request)
    if outcome is None:
        return receipt
    accepted = (receipt.show, receipt.screen, receipt.request, receipt.revision, receipt.epoch)
    observed = (outcome.show, outcome.screen, outcome.request, outcome.revision, outcome.epoch)
    if accepted != observed:
        return receipt
    return runtime.receipts.confirm_correction(outcome)


def opening_is_ready(runtime: Runtime, show: str) -> bool:
    for receipt in runtime.receipts.pending(show):
        if _observe(runtime, receipt).status == 'pending':
            return False
    return True


def correct(runtime: Runtime, show_id: str, screen: str,
            revision: str, request: str) -> CorrectionReceipt:
    existing = runtime.receipts.get_correction(show_id, request, required=False)
    if existing is not None:
        if (existing.screen, existing.revision) != (screen, revision):
            raise conflict('correction request already has a different screen or revision')
        existing = _observe(runtime, existing)
        if existing.status == 'confirmed':
            return existing
    show = runtime.live_show(show_id, screen)
    if existing is None:
        initial = runtime.editions.get(show.initial_revision)
        target = runtime.editions.get(revision)
        check_compatibility(initial, target)
        existing = runtime.receipts.accept_correction(show_id, screen, request, revision)
    for pending in runtime.receipts.pending(show_id):
        if pending.epoch > existing.epoch:
            break
        outcome = _observe(runtime, pending)
        if outcome.status == 'confirmed':
            continue
        runtime.screen.prepare_cutover(pending, runtime.editions.get(pending.revision))
        runtime.screen.apply_cutover(pending)
        if _observe(runtime, pending).status == 'pending':
            break
    return runtime.receipts.get_correction(show_id, request)


def correction_status(runtime: Runtime, show_id: str, request: str) -> CorrectionReceipt:
    return _observe(runtime, runtime.receipts.get_correction(show_id, request))
