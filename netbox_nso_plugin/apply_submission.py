# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Fence concurrent sends of one manual Apply attempt."""

from django.db import transaction

from .models import NSOApplyAttempt


def claim_send(attempt):
    """Commit a send sequence unless this attempt has a definite rejection."""
    with transaction.atomic():
        return claim_locked_send(NSOApplyAttempt.objects.select_for_update().get(pk=attempt.pk), attempt)


def claim_locked_send(locked, attempt):
    """Advance the send sequence of an attempt row that the caller holds locked."""
    if locked.response is not None and locked.http_status not in (200, 202):
        return False
    locked.send_seq += 1
    locked.save(update_fields=("send_seq",))
    attempt.send_seq = locked.send_seq
    return True


def record_response(attempt_id, *, http_status, response, send_seq=None):
    """Record a success or a rejection that rules out every possible admission."""
    admitted = http_status in (200, 202)
    with transaction.atomic():
        locked = NSOApplyAttempt.objects.select_for_update().get(pk=attempt_id)
        # A later rejection cannot rule out an earlier send whose answer was lost.
        if not admitted and (send_seq != 1 or locked.send_seq != send_seq):
            return False
        if locked.response is not None:
            if not admitted and locked.http_status in (200, 202):
                return False
            if admitted and locked.http_status not in (200, 202):
                raise RuntimeError("an Apply admission arrived after a recorded rejection")
            if locked.http_status != http_status or locked.response != response:
                raise RuntimeError("an Apply attempt received two different responses")
            return True
        locked.http_status = http_status
        locked.response = response
        locked.save(update_fields=("http_status", "response"))
    return True
