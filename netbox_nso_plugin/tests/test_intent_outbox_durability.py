# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""#1503 Appendix O (O1.2): the entry is the operator transaction's own row, or it is nothing.

The outbox exists because a scheduled push may not be lost, and what makes that true is
that the entry commits WITH the write it stands for. NetBox does not set
``ATOMIC_REQUESTS``, so a writer that opens no transaction of its own commits its row in
one transaction and its entry in another: a crash between them leaves owned intent with no
durable record and no drain candidate, which is exactly the loss the appendix removes.

So the two halves are pinned together here. A writer whose enqueue fails must leave NO
committed row either, and an ``enqueue`` outside a transaction is refused outright rather
than allowed to commit alone.
"""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TransactionTestCase
from django.urls import reverse

from ._outbox_case import entries, make_managed, without_commit_drain
from .mixins import IntentPushResetMixin, _CascadeFlushMixin

User = get_user_model()
PASSWORD = "outbox-durability-789"  # noqa: S105


class _DurabilityCase(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    """A managed device, an un-owned VLAN overlay, and an operator who may accept it."""

    tag = "dura"
    adapter_device_id = 7820

    def setUp(self):
        super().setUp()
        self.device, self.mgmt = make_managed(self.tag, self.adapter_device_id)
        self.user = User.objects.create_superuser(username="nsooutboxdurability", password=PASSWORD)
        self.client.force_login(self.user)
        self.state = self._own_imported_vlan(870)

    def _own_imported_vlan(self, vid):
        from ipam.models import VLAN

        from netbox_nso_plugin.models import NSOIntentOutboxEntry, NSOVLANState

        with without_commit_drain(), transaction.atomic():
            vlan = VLAN.objects.create(vid=vid, name=f"cl-{self.tag}-v{vid}")
            state = NSOVLANState.objects.create(management=self.mgmt, vlan=vlan, status="imported")
        NSOIntentOutboxEntry.objects.all().delete()
        return state


class TestAWriteAndItsEntryCommitTogether(_DurabilityCase):
    """codex O1 r3 F2(a): an accept that runs bare committed its row and lost the record."""

    def test_an_accept_whose_enqueue_fails_commits_nothing_at_all(self):
        from netbox_nso_plugin.models import NSOVLANState

        url = reverse("plugins:netbox_nso_plugin:vlan_accept", kwargs={"pk": self.state.pk})
        # The enqueue's first statement, failed the way a lost connection fails it.
        with patch("netbox_nso_plugin.outbox.current_txid", side_effect=RuntimeError("the write never landed")):
            try:
                self.client.post(url)
            except RuntimeError:
                pass
            else:
                raise AssertionError("the failed enqueue was swallowed instead of failing the write")

        assert NSOVLANState.objects.get(pk=self.state.pk).status == "imported", (
            "the row is owned with nothing durable to drain it"
        )
        assert entries(self.device, "vlan") == []

    def test_the_accept_commits_the_row_and_its_entry(self):
        from netbox_nso_plugin.models import NSOVLANState

        url = reverse("plugins:netbox_nso_plugin:vlan_accept", kwargs={"pk": self.state.pk})
        with without_commit_drain():
            assert self.client.post(url).status_code == 302

        assert NSOVLANState.objects.get(pk=self.state.pk).status != "imported"
        assert entries(self.device, "vlan"), "the accept recorded what the drain has to deliver"


class TestABareEnqueueIsRefused(_DurabilityCase):
    """codex O1 r3 F2(b): a site that never opens a transaction has to fail, not lose."""

    def test_appending_outside_a_transaction_raises(self):
        from django.db import connection

        from netbox_nso_plugin import outbox

        assert not connection.in_atomic_block, "this pin needs the autocommit a bare writer runs in"
        try:
            outbox.enqueue(self.device.pk, "vlan")
        except RuntimeError:
            pass
        else:
            raise AssertionError("the entry committed alone, so the write it stands for could be lost")

        assert entries(self.device, "vlan") == []
