# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Django-stack tests for template_content module.

These tests require the full NetBox/Django stack (run in devcontainer).
"""

import pathlib
import re
from unittest.mock import MagicMock, patch

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from django.test import SimpleTestCase, TestCase

from netbox_nso_plugin.models import NSOInterfaceState


class TestTemplateCommentSyntax(SimpleTestCase):
    """Guard against multiline ``{# #}`` comments, which Django renders literally."""

    def test_no_multiline_hash_comments(self):
        """Django's ``{# #}`` is single-line only; multiline leaks into the page.

        Use ``{% comment %}…{% endcomment %}`` for multiline instead.
        """
        templates_dir = pathlib.Path(__file__).resolve().parent.parent / "templates"
        # A comment open ``{#`` with no closing ``#}`` on the same line spans lines.
        offender = re.compile(r"\{#(?![^\n]*#\})")
        problems = []
        for path in templates_dir.rglob("*.html"):
            for lineno, line in enumerate(path.read_text().splitlines(), start=1):
                if offender.search(line):
                    problems.append(f"{path}:{lineno}: {line.strip()}")
        self.assertEqual(
            problems,
            [],
            "Multiline {# #} comments render as visible text; use {% comment %}:\n" + "\n".join(problems),
        )


class TestUpsertInterfaceStates(TestCase):
    """Tests for _upsert_interface_states helper function."""

    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="TCMfg", slug="tcmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="TCDev", slug="tcdev")
        role = DeviceRole.objects.create(name="TCRole", slug="tcrole")
        site = Site.objects.create(name="TCSite", slug="tcsite")
        cls.device = Device.objects.create(name="tc-router", device_type=dt, role=role, site=site)
        cls.interface = Interface.objects.create(device=cls.device, name="GigabitEthernet0/0", type="1000base-t")

    def test_empty_interfaces_returns_empty_dict(self):
        """No adapter interfaces → empty result dict."""
        from netbox_nso_plugin.template_content import _upsert_interface_states

        result = _upsert_interface_states(self.device, [])
        self.assertEqual(result, {})

    def test_interface_not_in_db_skipped(self):
        """Adapter interface not found in NetBox is skipped."""
        from netbox_nso_plugin.template_content import _upsert_interface_states

        result = _upsert_interface_states(
            self.device,
            [{"name": "NonExistentEth0/0", "attrs": {"description": {"nso_value": "test", "status": "changed"}}}],
        )
        self.assertEqual(result, {})

    def test_creates_new_interface_state(self):
        """First call creates an NSOInterfaceState row."""
        from netbox_nso_plugin.template_content import _upsert_interface_states

        NSOInterfaceState.objects.filter(interface=self.interface, attribute="description").delete()

        result = _upsert_interface_states(
            self.device,
            [
                {
                    "name": "GigabitEthernet0/0",
                    "attrs": {"description": {"nso_value": "new-desc", "status": "imported"}},
                }
            ],
        )
        self.assertIn(("GigabitEthernet0/0", "description"), result)
        state = result[("GigabitEthernet0/0", "description")]
        self.assertEqual(state.nso_value, "new-desc")

    def test_updates_existing_interface_state(self):
        """Second call updates nso_value and status."""
        from netbox_nso_plugin.template_content import _upsert_interface_states

        NSOInterfaceState.objects.filter(interface=self.interface, attribute="description").delete()
        state = NSOInterfaceState.objects.create(
            interface=self.interface,
            attribute="description",
            status="imported",
            nso_value="old-desc",
        )

        _upsert_interface_states(
            self.device,
            [
                {
                    "name": "GigabitEthernet0/0",
                    "attrs": {"description": {"nso_value": "updated-desc", "status": "changed"}},
                }
            ],
        )
        state.refresh_from_db()
        self.assertEqual(state.nso_value, "updated-desc")
        self.assertEqual(state.status, "changed")

    def test_none_interfaces_returns_empty(self):
        """None interfaces list returns empty dict."""
        from netbox_nso_plugin.template_content import _upsert_interface_states

        result = _upsert_interface_states(self.device, None)
        self.assertEqual(result, {})

    def test_last_apply_at_invalid_iso_ignored(self):
        """Invalid ISO timestamp in last_apply_at is silently ignored."""
        from netbox_nso_plugin.template_content import _upsert_interface_states

        NSOInterfaceState.objects.filter(interface=self.interface, attribute="description").delete()
        _upsert_interface_states(
            self.device,
            [
                {
                    "name": "GigabitEthernet0/0",
                    "attrs": {
                        "description": {
                            "nso_value": "x",
                            "status": "imported",
                            "last_apply_at": "not-a-date",
                        }
                    },
                }
            ],
        )
        state = NSOInterfaceState.objects.get(interface=self.interface, attribute="description")
        self.assertIsNone(state.last_apply_at)

    def test_last_apply_error_updated(self):
        """last_apply_error field is updated from adapter data."""
        from netbox_nso_plugin.template_content import _upsert_interface_states

        NSOInterfaceState.objects.filter(interface=self.interface, attribute="description").delete()
        _upsert_interface_states(
            self.device,
            [
                {
                    "name": "GigabitEthernet0/0",
                    "attrs": {
                        "description": {
                            "nso_value": "x",
                            "status": "apply_failed",
                            "last_apply_error": {"code": "nso_error", "message": "fail"},
                        }
                    },
                }
            ],
        )
        state = NSOInterfaceState.objects.get(interface=self.interface, attribute="description")
        self.assertIsNotNone(state.last_apply_error)

    def test_last_apply_at_updated_when_differs(self):
        """last_apply_at is updated when the adapter provides a new valid timestamp."""
        from netbox_nso_plugin.template_content import _upsert_interface_states

        NSOInterfaceState.objects.filter(interface=self.interface, attribute="description").delete()
        # Start with no last_apply_at
        NSOInterfaceState.objects.create(
            interface=self.interface,
            attribute="description",
            status="in_sync",
            nso_value="desc",
        )
        _upsert_interface_states(
            self.device,
            [
                {
                    "name": "GigabitEthernet0/0",
                    "attrs": {
                        "description": {
                            "nso_value": "desc",
                            "status": "in_sync",
                            "last_apply_at": "2025-01-01T12:00:00+00:00",
                        }
                    },
                }
            ],
        )
        state = NSOInterfaceState.objects.get(interface=self.interface, attribute="description")
        self.assertIsNotNone(state.last_apply_at)


class TestInterfaceNSOBadge(TestCase):
    """Tests for InterfaceNSOBadge.right_page()."""

    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="BadgeMfg", slug="badgemfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="BadgeDev", slug="badgedev")
        role = DeviceRole.objects.create(name="BadgeRole", slug="badgerole")
        site = Site.objects.create(name="BadgeSite", slug="badgesite")
        cls.device = Device.objects.create(name="badge-router", device_type=dt, role=role, site=site)
        cls.interface = Interface.objects.create(device=cls.device, name="Loopback0", type="virtual")
        cls.state = NSOInterfaceState.objects.create(
            interface=cls.interface, attribute="description", status="changed", nso_value="desc"
        )

    def test_right_page_returns_html(self):
        """right_page() renders the badge template and returns HTML string."""
        from netbox_nso_plugin.template_content import InterfaceNSOBadge

        badge = object.__new__(InterfaceNSOBadge)
        badge.context = {"object": self.interface, "request": MagicMock()}

        with patch.object(InterfaceNSOBadge, "render", return_value="<div>badge</div>") as mock_render:
            result = badge.right_page()

        mock_render.assert_called_once()
        call_args = mock_render.call_args
        self.assertIn("nso_states", call_args[1]["extra_context"])
        self.assertEqual(result, "<div>badge</div>")

    def test_right_page_passes_all_states(self):
        """right_page() passes all interface states keyed by attribute."""
        from netbox_nso_plugin.template_content import InterfaceNSOBadge

        NSOInterfaceState.objects.create(
            interface=self.interface, attribute="enabled", status="in_sync", nso_value="true"
        )

        badge = object.__new__(InterfaceNSOBadge)
        badge.context = {"object": self.interface, "request": MagicMock()}

        captured = {}

        def fake_render(template, extra_context=None):
            captured.update(extra_context or {})
            return ""

        with patch.object(InterfaceNSOBadge, "render", side_effect=fake_render):
            badge.right_page()

        states = captured.get("nso_states", {})
        self.assertIn("description", states)
        self.assertIn("enabled", states)

    def test_right_page_no_derived_intent_templates(self):
        """right_page() passes derived_intent_match=None when no templates configured."""
        from netbox_nso_plugin.template_content import InterfaceNSOBadge

        badge = object.__new__(InterfaceNSOBadge)
        badge.context = {"object": self.interface, "request": MagicMock()}

        captured = {}

        def fake_render(template, extra_context=None):
            captured.update(extra_context or {})
            return ""

        with patch("netbox_nso_plugin.template_content.apps") as mock_apps:
            cfg = MagicMock()
            cfg._derived_intent_templates = []
            mock_apps.get_app_config.return_value = cfg
            with patch.object(InterfaceNSOBadge, "render", side_effect=fake_render):
                badge.right_page()

        self.assertIsNone(captured.get("derived_intent_match"))

    def test_right_page_managed_auto_sentinel(self):
        """right_page() passes derived_intent_match with sentinel when description matches."""
        from netbox_nso_plugin.derived_intent import SentinelTemplate
        from netbox_nso_plugin.template_content import InterfaceNSOBadge

        self.interface.description = "[auto] link to peer"
        self.interface.save(update_fields=["description"])

        badge = object.__new__(InterfaceNSOBadge)
        badge.context = {"object": self.interface, "request": MagicMock()}

        captured = {}

        def fake_render(template, extra_context=None):
            captured.update(extra_context or {})
            return ""

        template = SentinelTemplate(sentinel="[auto]", template="[auto] {peer_device}:{peer_iface}")
        with patch("netbox_nso_plugin.template_content.apps") as mock_apps:
            cfg = MagicMock()
            cfg._derived_intent_templates = [template]
            mock_apps.get_app_config.return_value = cfg
            with patch.object(InterfaceNSOBadge, "render", side_effect=fake_render):
                badge.right_page()

        match = captured.get("derived_intent_match")
        self.assertIsNotNone(match)
        self.assertEqual(match.sentinel, "[auto]")

    def test_right_page_unmanaged_description_no_match(self):
        """right_page() passes derived_intent_match=None when description is unmanaged."""
        from netbox_nso_plugin.derived_intent import SentinelTemplate
        from netbox_nso_plugin.template_content import InterfaceNSOBadge

        self.interface.description = "manually configured"
        self.interface.save(update_fields=["description"])

        badge = object.__new__(InterfaceNSOBadge)
        badge.context = {"object": self.interface, "request": MagicMock()}

        captured = {}

        def fake_render(template, extra_context=None):
            captured.update(extra_context or {})
            return ""

        template = SentinelTemplate(sentinel="[auto]", template="[auto] {peer_device}:{peer_iface}")
        with patch("netbox_nso_plugin.template_content.apps") as mock_apps:
            cfg = MagicMock()
            cfg._derived_intent_templates = [template]
            mock_apps.get_app_config.return_value = cfg
            with patch.object(InterfaceNSOBadge, "render", side_effect=fake_render):
                badge.right_page()

        self.assertIsNone(captured.get("derived_intent_match"))
