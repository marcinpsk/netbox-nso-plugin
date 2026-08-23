# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""CR-P16: an SNMPv3 trap host is pushable — and the version-grain trap that made it unsafe.

A v3 trap host used to be a dead end. It imported, it displayed, and it could never be applied:
both NSO host writers KEY the receiver on the security user name (IOS puts it in the
`community-string` leaf; IOS-XR makes it the third key component), and nothing in the stack carried
that name. The overlay had `community_hash` — which is v1/v2c only — so the push had nothing to put
in the field. CR-P4 made the refusal LOUD rather than a server-side log line, which was right, but
the capability was simply absent.

network-state-export now exports `host/user` for v3 hosts (it is not a secret — it is the same
identity the v3-user list already publishes; the community on a v1/v2c host, which lives in the very
same NED field, IS a secret and is still never exported). The adapter mirrors it, the reconciler
stores it, and the push sends it.

Closing that turned up a second, quieter bug in the refusal itself — see
`test_the_refusal_fires_for_the_grain_a_REAL_device_produces`.
"""

from __future__ import annotations

from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.test import TestCase

from netbox_nso_plugin.delivery import deliver
from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSOSnmpHostState
from netbox_nso_plugin.signals import snmp_host_push_blocker

from .mixins import IntentPushResetMixin


class _HostBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="V3Mfg", slug="v3mfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="V3Dev", slug="v3dev")
        role = DeviceRole.objects.create(name="V3Role", slug="v3role")
        site = Site.objects.create(name="V3Site", slug="v3site")
        cls.device = Device.objects.create(name="v3-rtr", device_type=dt, role=role, site=site)

    def setUp(self):
        super().setUp()
        inst, _ = NSOInstance.objects.get_or_create(name="v3-inst", defaults={"adapter_instance_id": "v3-inst"})
        self.mgmt = NSODeviceManagement.objects.get_or_create(
            device=self.device,
            defaults={
                "nso_instance": inst,
                "nso_device_name": "nso-v3-rtr",
                "adapter_device_id": 42,
                "manage_snmp": True,
            },
        )[0]


class TestSnmpV3HostPush(IntentPushResetMixin, _HostBase):
    def _push(self):
        from netbox_nso_plugin.signals import reset_intent_push_state

        reset_intent_push_state()
        with patch("netbox_nso_plugin.adapter_client.put_snmp_intent") as mock_put:
            deliver("snmp", self.device.pk, self.mgmt.adapter_device_id)
        mock_put.assert_called_once()
        self.assertEqual(mock_put.call_args.args[0], self.mgmt.adapter_device_id)
        return mock_put.call_args[0][3]  # hosts

    def test_push_uses_current_management_adapter_device_id(self):
        self.mgmt.adapter_device_id = 43
        self.mgmt.save(update_fields=["adapter_device_id"])
        NSOSnmpHostState.objects.create(
            management=self.mgmt,
            address="198.18.0.5",
            version="3",
            notify_type="trap",
            username="netmon-v3",
            status="accepted",
        )

        self._push()

    def test_a_v3_host_WITH_a_user_name_is_pushed(self):
        """The feature. The user name goes into community_or_user — the field both writers key on."""
        NSOSnmpHostState.objects.create(
            management=self.mgmt,
            address="10.0.0.5",
            version="3",
            notify_type="trap",
            username="netmon-v3",
            status="accepted",
        )
        hosts = self._push()

        self.assertEqual(len(hosts), 1)
        self.assertEqual(hosts[0]["community_or_user"], "netmon-v3")
        self.assertEqual(hosts[0]["address"], "10.0.0.5")

    def test_a_v3_host_WITHOUT_a_user_name_is_still_refused(self):
        """The half that must stay refused: with no user name there is nothing to key the receiver
        on. IOS-XR cannot even form the key; IOS would write a host bound to no user at all. A push
        is worse than a refusal here, so the row is surfaced as an error instead.
        """
        row = NSOSnmpHostState.objects.create(
            management=self.mgmt, address="10.0.0.7", version="3", notify_type="trap", status="accepted"
        )
        hosts = self._push()

        self.assertEqual(hosts, [])
        row.refresh_from_db()
        self.assertEqual(row.status, "error")
        self.assertIn("no security user name", snmp_host_push_blocker(row))

    def test_the_refusal_fires_for_the_grain_a_REAL_device_produces(self):
        """The quiet bug closing this one uncovered.

        The reconciler stores `version` VERBATIM from the adapter, which carries the NED's grain —
        `"3"` — while the plugin's own forms and fixtures say `"v3"`. The refusal was written as
        `row.version == "v3"`, so it matched a hand-created row and NEVER an imported one: every v3
        trap host actually read off a device sailed straight past the guard and was pushed with an
        EMPTY community_or_user, keying the receiver on no user at all. The only reason it never
        bit is that nothing downstream could push a v3 host anyway — and this change removes that
        accident.

        Both spellings must be recognised, or the guard protects only the case that never occurs.
        """
        for grain in ("3", "v3", "V3", "snmpv3"):
            row = NSOSnmpHostState(management=self.mgmt, address="10.0.0.8", version=grain, notify_type="trap")
            self.assertNotEqual(snmp_host_push_blocker(row), "", f"version {grain!r} slipped past the v3 guard")
            row.username = "netmon-v3"
            self.assertEqual(snmp_host_push_blocker(row), "", f"version {grain!r} was refused despite a user name")

    def test_a_v2c_host_still_pushes_its_COMMUNITY_not_a_user_name(self):
        """The same intent field carries two different things. A v2c host must keep sending its
        community reference — if the v3 branch leaked into it, the host would be bound to a
        community that does not exist.
        """
        NSOSnmpHostState.objects.create(
            management=self.mgmt,
            address="10.0.0.6",
            version="2c",
            notify_type="trap",
            community_hash="abc123def456",
            username="should-be-ignored",  # a stale value must not win on a v2c host
            status="accepted",
        )
        hosts = self._push()

        self.assertEqual(hosts[0]["community_or_user"], "abc123def456")


class TestSnmpV3HostReconcile(_HostBase):
    """The read half: the user name has to survive the trip from the device into the overlay."""

    def _reconcile(self, hosts):
        from netbox_nso_plugin.template_content import _reconcile_snmp_config

        _reconcile_snmp_config(self.device, {"communities": [], "v3_users": [], "hosts": hosts, "system_info": None})
        return NSOSnmpHostState.objects.get(management=self.mgmt)

    def test_an_imported_v3_host_keeps_its_user_name(self):
        row = self._reconcile(
            [{"address": "10.0.0.5", "version": "3", "notify_type": "inform", "username": "netmon-v3"}]
        )
        self.assertEqual(row.username, "netmon-v3")
        # and it is immediately pushable — which is the whole point
        self.assertEqual(snmp_host_push_blocker(row), "")

    def test_an_imported_v2c_host_has_no_user_name(self):
        """The export gates the leaf on version precisely so a v1/v2c host's COMMUNITY — which sits
        in the very same NED field — can never arrive here. Nothing to store, nothing to leak.
        """
        row = self._reconcile([{"address": "10.0.0.6", "version": "2c", "notify_type": "trap"}])
        self.assertEqual(row.username, "")
