# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""#1502 Appendix S (S6) — what may become ``deploying``, and what may not stay there.

Pins S6.3, S6.4 and S6.5 (R3 P5.13). A static-route row is settled by a generation-correlated
result and by nothing else, which puts two obligations on the ``deploying`` state itself:

* a row may only enter it when the adapter is actually holding the intent it will settle
  against — an Apply whose forced push was refused would otherwise mint a row no result can
  ever name (S6.3);
* a row may not stay in it forever. The backstop escalates on the generation clock (S6.4),
  and a row with **no** clock — the state an upgrade leaves behind, and an impossible one
  after the rollout backfill — escalates with its own reason instead of being skipped by a
  NULL-false comparison (S6.5).
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from ._outbox_case import mirror_update
from ._settlement_case import _make_device

# ── CodeQL py/stack-trace-exposure — the refusal wording is rebuilt, never serialized ────


class TestApplyRefusalSealing(TestCase):
    """PR #24 CodeQL alert 18: no exception object may flow into an HTTP response."""

    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth import get_user_model

        cls.superuser = get_user_model().objects.create_superuser(
            username="sealtestnsoadmin", password="seal-test-pass", email="seal@test.example"
        )

    def setUp(self):
        super().setUp()
        self.client.force_login(self.superuser)

    def _mgmt(self, tag, adapter_device_id):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        device = _make_device(tag)
        inst, _ = NSOInstance.objects.get_or_create(name=f"{tag}-inst", defaults={"adapter_instance_id": f"{tag}-inst"})
        with patch("netbox_nso_plugin.signals._sync_committed_scope_to_adapter"):
            return NSODeviceManagement.objects.create(
                device=device,
                nso_instance=inst,
                nso_device_name=f"nso-{tag}",
                adapter_device_id=adapter_device_id,
            )

    def test_the_refusal_handler_never_serializes_the_exception(self):
        """The ApplyRefused handler rebuilds its wording; the exception reaches only the log."""
        import ast
        import inspect

        from netbox_nso_plugin import views

        def names_apply_refused(handler):
            """True for ``except ApplyRefused`` and for a tuple form that includes it.

            Matching only the bare Name let ``except (ApplyRefused, OtherError)`` past the
            pin, which is the shape a later handler is most likely to grow into.
            """
            node = handler.type
            if isinstance(node, ast.Tuple):
                return any(isinstance(elt, ast.Name) and elt.id == "ApplyRefused" for elt in node.elts)
            return isinstance(node, ast.Name) and node.id == "ApplyRefused"

        handlers = [
            node
            for node in ast.walk(ast.parse(inspect.getsource(views)))
            if isinstance(node, ast.ExceptHandler) and names_apply_refused(node)
        ]
        assert handlers, "the apply action lost its ApplyRefused handler"
        for handler in handlers:
            for call in (node for node in ast.walk(handler) if isinstance(node, ast.Call)):
                target = ast.unparse(call.func)
                if target in {"JsonResponse", "messages.error", "messages.warning", "messages.success"}:
                    names = {n.id for n in ast.walk(call) if isinstance(n, ast.Name)}
                    assert handler.name not in names, (
                        f"{target} in the ApplyRefused handler uses the bound exception; "
                        "rebuild the message from the refusal type instead"
                    )

    def test_no_refusal_raise_site_carries_wording(self):
        """A refusal carries delivery keys and vocabulary constants, and nothing else.

        This is what makes the handler safe by construction: an exception that holds no text
        cannot serialize any into a response, whichever renderer reads it.
        """
        import ast
        import inspect

        from netbox_nso_plugin import delivery, views

        tree = ast.parse(inspect.getsource(views))
        refusals = {"ApplyRefused"} | {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef)
            and any(isinstance(base, ast.Name) and base.id == "ApplyRefused" for base in node.bases)
        }
        keys = set(delivery.delivery_keys())

        raised = [
            node.exc
            for node in ast.walk(tree)
            if isinstance(node, ast.Raise)
            and isinstance(node.exc, ast.Call)
            and isinstance(node.exc.func, ast.Name)
            and node.exc.func.id in refusals
        ]
        assert raised, "the apply preparation lost its typed refusals"
        for call in raised:
            for node in ast.walk(call):
                assert not isinstance(node, ast.JoinedStr), (
                    f"{call.func.id} is raised with interpolated wording; pass typed fields instead"
                )
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    assert node.value in keys, (
                        f"{call.func.id} is raised with the literal {node.value!r}; a refusal may "
                        "carry a delivery key or a vocabulary constant, and the handler says the words"
                    )

    def test_the_renderer_answers_the_refusals_no_end_to_end_case_reaches(self):
        """The promotion failure renders its fixed wording, and an untyped refusal stays mute."""
        from netbox_nso_plugin.views import (
            _APPLY_PROMOTION_MESSAGE,
            _APPLY_REFUSED_MESSAGE,
            ApplyPromotionFailed,
            ApplyRefused,
            _apply_refusal_message,
        )

        mgmt = self._mgmt("seal-render", 99)

        assert _apply_refusal_message(ApplyPromotionFailed(), mgmt) == _APPLY_PROMOTION_MESSAGE
        assert _apply_refusal_message(ApplyRefused("text a raise site should not carry"), mgmt) == (
            _APPLY_REFUSED_MESSAGE
        )

    def test_a_refused_snmp_refresh_answers_the_rebuilt_wording(self):
        """End to end: POST to view to 409 never exposes the persisted exception text."""
        from django.urls import reverse

        from netbox_nso_plugin import drain
        from netbox_nso_plugin.views import _snmp_refusal_message

        mgmt = self._mgmt("seal-snmp", 97)
        supplied = "Traceback: private adapter path and response body"
        mirror_update(mgmt, intent_push_errors={"snmp": {"code": "nso_error", "message": supplied}})
        for name, answer in (("push_now", {"count": 0}), ("drain_key", drain.REFUSED)):
            patcher = patch(f"netbox_nso_plugin.drain.{name}", side_effect=lambda *a, answer=answer, **kw: answer)
            patcher.start()
            self.addCleanup(patcher.stop)

        url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=[mgmt.pk, "apply"])
        response = self.client.post(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

        assert response.status_code == 409
        mgmt.refresh_from_db()
        assert response.json()["message"] == _snmp_refusal_message(mgmt)
        assert supplied not in response.json()["message"]
        assert "The NSO adapter request failed. See the server log." in response.json()["message"]

    def test_an_expired_budget_answers_the_deadline_wording(self):
        """End to end: the deadline refusal serves its fixed wording with a 409."""
        from itertools import chain, repeat

        from django.urls import reverse

        from netbox_nso_plugin import drain
        from netbox_nso_plugin.views import _APPLY_DEADLINE_MESSAGE

        mgmt = self._mgmt("seal-deadline", 98)
        spent = drain.SEND_DEADLINE.total_seconds() + 1
        with patch("netbox_nso_plugin.drain._send_clock", side_effect=chain([0, spent], repeat(spent))):
            url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=[mgmt.pk, "apply"])
            response = self.client.post(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

        assert response.status_code == 409
        assert response.json()["message"] == _APPLY_DEADLINE_MESSAGE
