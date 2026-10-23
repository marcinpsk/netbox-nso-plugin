# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The NativeWriter, its scopes and guards, and the static write-site census (#1717 increment I2).

Inside a request every native write needs its exact writer permit (C9), a swallowed denial
still refuses the request (latch), and no adapter call runs in the mutation phase. A system
entry writes device facts only (C14). The tests use the real ORM and real ObjectPermission rows.
"""

import ast
import pathlib
from contextlib import contextmanager, suppress
from uuid import uuid4

from dcim.models import Interface
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import SimpleTestCase, TestCase
from utilities.exceptions import PermissionsViolation

import netbox_nso_plugin
from netbox_nso_plugin import native_authorization as native
from netbox_nso_plugin.models import (
    NSOBFDInterfaceState,
    NSOISISInterfaceState,
    NSOSwitchportState,
)
from netbox_nso_plugin.native_authorization import (
    Anchored,
    Dependency,
    NativeWriteRefused,
    NoNativeIntentEffects,
    Observed,
    OwnedIntent,
    OwnedIntentRefused,
    authority_scope,
)

from ._authority_case import FakeAdapter, operator
from ._outbox_case import make_managed, without_commit_drain
from ._ownership_case import acquire_overlay
from .mixins import IntentPushResetMixin

User = get_user_model()
_PLUGIN_ROOT = pathlib.Path(netbox_nso_plugin.__file__).parent


# ── Structure and census ──


class TestOwnedEffectCensus(SimpleTestCase):
    """Every overlay scope states which native effects carry its intent (C12/C14)."""

    def test_every_overlay_model_declares_its_owned_native_effects(self):
        from netbox_nso_plugin.intent_state import OVERLAY_MODEL_RANKS

        self.assertEqual(native.missing_owned_effect_declarations(OVERLAY_MODEL_RANKS), [])

    def test_census_rejects_an_undeclared_overlay_model(self):
        self.assertEqual(
            native.missing_owned_effect_declarations(["netbox_nso_plugin.nsobfdinterfacestate", "netbox_nso_plugin.x"]),
            ["netbox_nso_plugin.x"],
        )

    def test_entry_kinds_are_pinned(self):
        kinds = {
            scope: tuple(
                (
                    type(entry).__name__,
                    entry.native_model,
                    getattr(entry, "fields", None),
                    getattr(entry, "lifecycle", None),
                )
                for entry in entries
            )
            for scope, entries in native.OWNED_NATIVE_EFFECTS.items()
            if not isinstance(entries, NoNativeIntentEffects)
        }
        self.assertEqual(
            kinds,
            {
                "netbox_nso_plugin.nsobfdinterfacestate": (
                    ("Anchored", "netbox_routing.bfdinterface", None, True),
                    ("Dependency", "netbox_routing.bfdprofile", None, None),
                ),
                "netbox_nso_plugin.nsoisisinterfacestate": (
                    ("Anchored", "netbox_routing.isisinterface", frozenset({"instance"}), False),
                    ("Dependency", "netbox_routing.isisinstance", None, None),
                ),
                "netbox_nso_plugin.nsoredistributionstate": (
                    (
                        "Anchored",
                        "netbox_routing.redistribution",
                        frozenset({"route_map", "metric", "metric_type"}),
                        True,
                    ),
                ),
                "netbox_nso_plugin.nsoswitchportstate": (
                    ("Anchored", "dcim.interface", frozenset({"mode", "untagged_vlan", "tagged_vlans"}), False),
                ),
                "netbox_nso_plugin.nsointerfacemtustate": (("Anchored", "dcim.interface", frozenset({"mtu"}), False),),
            },
        )
        entries = [
            entry for value in native.OWNED_NATIVE_EFFECTS.values() if isinstance(value, tuple) for entry in value
        ]
        self.assertTrue(all(isinstance(entry, Anchored | Dependency) for entry in entries))


def _plugin_sources():
    for path in sorted(_PLUGIN_ROOT.rglob("*.py")):
        relative = path.relative_to(_PLUGIN_ROOT).as_posix()
        if not relative.startswith(("tests/", "migrations/")):
            yield relative, ast.parse(path.read_text(encoding="utf-8"), filename=relative)


def _calls(tree, names):
    """Yield (qualified function, call name) for every call of one of *names*."""
    found = []

    def visit(node, scope):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                visit(child, (*scope, child.name))
                continue
            if isinstance(child, ast.Call):
                func = child.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if name in names:
                    found.append((".".join(scope), name))
            visit(child, scope)

    visit(tree, ())
    return found


class TestWriteSiteStructure(SimpleTestCase):
    """Static guards with stated limits: they pin today's sites, so a new one needs review."""

    def test_no_system_scope_write_site_declares_owned_intent(self):
        """Owned intent reaches native rows only through the operator writer (C14): no site declares it."""
        sites = [
            f"{module}:{function}"
            for module, tree in _plugin_sources()
            if module != "native_authorization.py"
            for function, _name in _calls(tree, {"OwnedIntent"})
        ]
        self.assertEqual(sites, [])

    def test_raw_sql_sites_are_the_allowlist(self):
        """No raw SQL DML against native tables (C9b): each raw SQL site is reviewed and pinned.

        Limit: the scan matches call names, so an aliased cursor method escapes it.
        """
        found = sorted(
            f"{module}:{function}:{name}"
            for module, tree in _plugin_sources()
            for function, name in _calls(tree, {"execute", "executemany", "raw", "extra", "RawSQL"})
        )
        self.assertEqual(found, sorted(_RAW_SQL_ALLOWLIST))


class TestUnverifiedEndpointCensus(SimpleTestCase):
    """Only the allocation endpoints still write native rows outside the NativeWriter (increment I3)."""

    def test_the_unverified_endpoints_are_pinned(self):
        from netbox_nso_plugin import urls
        from netbox_nso_plugin.api import urls as api_urls
        from netbox_nso_plugin.request_authorization import EndpointPolicy, view_class_of

        found = set()
        for pattern in (*urls.urlpatterns, *api_urls.urlpatterns):
            cls = view_class_of(pattern.callback)
            policy = getattr(cls, "policy", None)
            if policy is None:
                continue
            policies = [policy] if isinstance(policy, EndpointPolicy) else list(policy.values())
            if any(item.effects == "unverified_native_mutation" for item in policies):
                found.add(cls.__qualname__)
        self.assertEqual(found, {"NSOAutoAssignIPView", "NSOProvisionLinkRoleView"})


# Advisory locks, sequence reads, transaction settings and plugin-table upserts; the two
# execute-wrapper callbacks forward a statement without writing one.
_RAW_SQL_ALLOWLIST = (
    "apply_state.py:lock_device_intent_transaction:execute",
    "apply_state.py:lock_shared_dependencies:execute",
    "deployment.py:_exclusive_transition_pending:execute",
    "deployment.py:lock_mutation:execute",
    "deployment.py:operation:execute",
    "deployment.py:operation:execute",
    "deployment.py:operation:execute",
    "deployment.py:exclusive_transition:execute",
    "deployment.py:exclusive_transition:execute",
    "deployment.py:exclusive_transition:execute",
    "deployment_probe.py:locked_gate:execute",
    "deployment_probe.py:maintenance_gate:execute",
    "deployment_probe.py:maintenance_gate:execute",
    "drain.py:_db_now:execute",
    "drain.py:_repeatable_read:execute",
    "drain.py:reset_offboard_state:execute",
    "drain.py:_record_capture_refusal:execute",
    "intent_generation.py:allocate_intent_generation:execute",
    "intent_state.py:renderer_query_trace.trace:execute",
    "intent_state.py:mirror_transaction:execute",
    "native_authorization.py:_backstop:execute",
    "outbox.py:allocate_push_seq:execute",
    "outbox.py:_issued_push_seq:execute",
    "outbox.py:advance_push_seq:execute",
    "outbox.py:current_txid:execute",
    "outbox.py:bump_intent_revision:execute",
    "read_gate.py:enqueue_reconcile_carrier:execute",
    "read_gate.py:enqueue_reconcile_carrier:execute",
    "restore.py:advance_static_route_pk:execute",
    "restore.py:advance_static_route_pk:execute",
    "restore.py:advance_static_route_pk:execute",
    "switching_preparation.py:_version:execute",
)


# ── The guards in an operator scope (C9) and the latch ──


def _plan_save(instance, fields, *, origin=None, grant=None):
    """Run one exact native save through the renderer writer, as the views and reconcilers do."""
    from netbox_nso_plugin.renderer_writer import (
        RendererMutationPlan,
        planned_save,
        renderer_mirror_writes,
        renderer_writes,
    )

    plan = RendererMutationPlan.build(saves=(planned_save(instance, update_fields=fields, origin=origin),), grant=grant)
    mutation = renderer_writes(plan) if plan.changes_content else renderer_mirror_writes(plan)
    with mutation as writer:
        writer.save(instance, update_fields=fields)


@contextmanager
def _system_scope():
    """A request's read scope with one allowlisted system entry running, as a refresh runs."""
    holder = {}

    def entry(body):
        holder["value"] = body()

    wrapped = native.system_entry("test")(entry)
    try:
        with authority_scope("read"):
            yield lambda body: (wrapped(body), holder.get("value"))[1]
    finally:
        native.SYSTEM_ENTRIES.pop(f"{entry.__module__}.{entry.__qualname__}", None)


class TestOperatorScopeGuards(IntentPushResetMixin, TestCase):
    """Inside an operator scope every native write needs its exact NativeWriter permit."""

    @classmethod
    def setUpTestData(cls):
        cls.device, cls.management = make_managed("nw-guard", 17181)
        cls.first = Interface.objects.create(device=cls.device, name="ge-0/0/1", type="1000base-t", description="one")
        cls.second = Interface.objects.create(device=cls.device, name="ge-0/0/2", type="1000base-t", description="two")
        cls.admin = User.objects.create_superuser(username=f"native-admin-{uuid4().hex[:6]}")

    def assertRefused(self, write):
        with self.assertRaises(NativeWriteRefused), authority_scope("native_mutation", self.admin):
            write()

    def test_a_direct_native_save_is_refused(self):
        self.first.description = "direct"
        self.assertRefused(self.first.save)
        self.first.refresh_from_db()
        self.assertEqual(self.first.description, "one")

    def test_a_native_queryset_update_or_bulk_write_is_refused(self):
        self.assertRefused(lambda: Interface.objects.filter(pk=self.first.pk).update(description="update"))
        self.assertRefused(
            lambda: Interface.objects.bulk_create([Interface(device=self.device, name="ge-0/0/9", type="virtual")])
        )
        self.first.refresh_from_db()
        self.assertEqual(self.first.description, "one")

    def test_raw_native_dml_is_refused_by_the_sql_backstop(self):
        def raw():
            with connection.cursor() as cursor:
                cursor.execute("UPDATE dcim_interface SET description = %s WHERE id = %s", ["raw", self.first.pk])

        self.assertRefused(raw)
        self.first.refresh_from_db()
        self.assertEqual(self.first.description, "one")

    def test_a_signal_born_write_needs_its_own_permit(self):
        """An untracked write made by a signal handler during a permitted save is refused (C9a)."""
        from dcim.models import Site
        from django.db.models.signals import post_save

        def write_another_row(sender, instance, **kwargs):
            if instance.pk == self.first.pk:
                Site.objects.filter(pk=self.device.site_id).update(description="signal")

        post_save.connect(write_another_row, sender=Interface, dispatch_uid="nw-signal-born")
        self.addCleanup(post_save.disconnect, sender=Interface, dispatch_uid="nw-signal-born")
        candidate = Interface.objects.get(pk=self.first.pk)
        candidate.description = "permitted"
        self.assertRefused(lambda: _plan_save(candidate, ("description",)))
        self.first.refresh_from_db()
        self.assertEqual(self.first.description, "one")

    def _during_first_save(self, write):
        """Run *write* from a signal handler while the first interface's permitted save runs."""
        from django.db.models.signals import post_save

        def handler(sender, instance, **kwargs):
            if instance.pk == self.first.pk:
                write()

        post_save.connect(handler, sender=Interface, dispatch_uid="nw-during-save")
        try:
            candidate = Interface.objects.get(pk=self.first.pk)
            candidate.description = "during"
            self.assertRefused(lambda: _plan_save(candidate, ("description",)))
        finally:
            post_save.disconnect(sender=Interface, dispatch_uid="nw-during-save")

    def test_signal_born_m2m_edges_of_another_owner_are_refused(self):
        """Through-table writes during A's permitted save: bulk, direct create, delete, upsert (C9a)."""
        from ipam.models import VLAN

        vlan, other = VLAN.objects.create(vid=3002, name="nw-edge"), VLAN.objects.create(vid=3003, name="nw-edge-2")
        through = Interface.tagged_vlans.through
        edge = through.objects.create(interface_id=self.second.pk, vlan_id=vlan.pk)
        writes = {
            "bulk create": lambda: through.objects.bulk_create(
                [through(interface_id=self.second.pk, vlan_id=other.pk)]
            ),
            "direct create": lambda: through.objects.create(interface_id=self.second.pk, vlan_id=other.pk),
            "delete": lambda: through.objects.filter(interface_id=self.second.pk).delete(),
            "upsert": lambda: through.objects.bulk_create(
                [through(pk=edge.pk, interface_id=self.first.pk, vlan_id=other.pk)],
                update_conflicts=True,
                update_fields=["vlan"],
                unique_fields=["pk"],
            ),
        }
        for name, write in writes.items():
            with self.subTest(write=name):
                self._during_first_save(write)
        self.assertEqual(
            list(through.objects.filter(interface_id=self.second.pk).values_list("vlan_id", flat=True)), [vlan.pk]
        )

    def test_a_positional_upsert_inside_an_m2m_permit_is_refused(self):
        """update_conflicts passed by position is still an upsert of another owner's stored row."""
        from ipam.models import VLAN

        vlan, other = VLAN.objects.create(vid=3006, name="nw-upsert"), VLAN.objects.create(vid=3007, name="nw-upsert-2")
        through = Interface.tagged_vlans.through
        edge = through.objects.create(interface_id=self.second.pk, vlan_id=vlan.pk)

        def upsert():
            with native.active_native_writer().m2m(self.first, "tagged_vlans", origin=None):
                through.objects.bulk_create(
                    [through(pk=edge.pk, interface_id=self.first.pk, vlan_id=other.pk)],
                    None,
                    False,
                    True,
                    ["vlan"],
                    ["pk"],
                )

        self.assertRefused(upsert)
        edge.refresh_from_db()
        self.assertEqual((edge.interface_id, edge.vlan_id), (self.second.pk, vlan.pk))

    def test_a_signal_born_m2m_edge_of_the_same_owner_is_refused(self):
        """A direct through insert is no M2M change of the owner: it needs an M2M permit."""
        from ipam.models import VLAN

        vlan = VLAN.objects.create(vid=3004, name="nw-own-edge")
        through = Interface.tagged_vlans.through
        self._during_first_save(lambda: through.objects.create(interface_id=self.first.pk, vlan_id=vlan.pk))
        self.assertFalse(through.objects.filter(interface_id=self.first.pk).exists())

    def test_the_permitted_write_succeeds(self):
        candidate = Interface.objects.get(pk=self.first.pk)
        candidate.description = "permitted"
        with authority_scope("native_mutation", self.admin):
            _plan_save(candidate, ("description",))
        self.first.refresh_from_db()
        self.assertEqual(self.first.description, "permitted")

    def test_an_adapter_call_inside_the_mutation_phase_is_refused(self):
        """Phase rule: after the first native effect no adapter call runs until the scope commits."""
        from unittest.mock import patch

        from netbox_nso_plugin import adapter_client

        candidate = Interface.objects.get(pk=self.first.pk)
        candidate.description = "phase"

        def write_then_read():
            _plan_save(candidate, ("description",))
            adapter_client.get_bfd(17181)

        FakeAdapter.calls = []
        with patch("netbox_nso_plugin.adapter_client.requests.Session", FakeAdapter):
            self.assertRefused(write_then_read)
            self.assertRefused(lambda: adapter_client.sync_notify(17181))
        self.assertEqual(FakeAdapter.calls, [])
        self.first.refresh_from_db()
        self.assertEqual(self.first.description, "one")

    def _create_entry(self, user, name):
        """Create a route-map entry and add its match list in one operation, as policy materialize does."""
        from netbox_routing.models import PrefixList, RouteMap, RouteMapEntry

        from netbox_nso_plugin.renderer_writer import (
            RendererMutationPlan,
            planned_m2m_add,
            planned_save,
            renderer_mirror_writes,
            renderer_writes,
        )

        prefix_list = PrefixList.objects.create(name=f"nw-pl-{name}")
        entry = RouteMapEntry(route_map=RouteMap.objects.create(name=name), sequence=10, action="permit")
        plan = RendererMutationPlan.build(
            saves=(planned_save(entry, force_insert=True, natural_key=("route_map", "sequence")),),
            m2m_writes=(planned_m2m_add(entry, "match_prefix_list", (prefix_list,)),),
        )
        with authority_scope("native_mutation", user):
            with (renderer_writes if plan.changes_content else renderer_mirror_writes)(plan) as writer:
                writer.save(entry, force_insert=True)
                writer.m2m_add(entry, "match_prefix_list", (prefix_list,))
        return prefix_list

    def test_a_row_created_in_the_operation_needs_only_add(self):
        """C1: the M2M change of a row this operation created needs no change permission."""
        from netbox_routing.models import RouteMapEntry

        prefix_list = self._create_entry(operator((("add",), RouteMapEntry, None)), "nw-created-map")
        entry = RouteMapEntry.objects.get(route_map__name="nw-created-map")
        self.assertEqual(list(entry.match_prefix_list.all()), [prefix_list])

    def test_a_created_row_outside_the_add_constraint_is_refused_at_phase_end(self):
        from netbox_routing.models import RouteMapEntry

        user = operator((("add",), RouteMapEntry, {"sequence": 20}))
        with self.assertRaises(PermissionsViolation):
            self._create_entry(user, "nw-refused-map")
        self.assertFalse(RouteMapEntry.objects.filter(route_map__name="nw-refused-map").exists())

    def test_a_swallowed_denial_still_refuses_the_request(self):
        """The failure latch: a helper that catches the denial cannot commit the request (D9)."""
        user = operator((("change",), Interface, {"name": self.first.name}))
        allowed = Interface.objects.get(pk=self.first.pk)
        allowed.description = "allowed"
        denied = Interface.objects.get(pk=self.second.pk)
        denied.description = "denied"
        with self.assertRaises(PermissionsViolation), authority_scope("native_mutation", user):
            _plan_save(allowed, ("description",))
            with suppress(PermissionsViolation):
                _plan_save(denied, ("description",))
        self.first.refresh_from_db()
        self.second.refresh_from_db()
        self.assertEqual((self.first.description, self.second.description), ("one", "two"))


class TestSystemScope(IntentPushResetMixin, TestCase):
    """A system entry writes device facts; owned intent never reaches a native row through it (C14)."""

    @classmethod
    def setUpTestData(cls):
        from netbox_routing.models import ISISInstance, ISISInterface

        cls.device, cls.management = make_managed("nw-system", 17182)
        cls.interface = Interface.objects.create(device=cls.device, name="ge-0/0/3", type="1000base-t")
        cls.other = Interface.objects.create(device=cls.device, name="ge-0/0/4", type="1000base-t")
        cls.current = ISISInstance.objects.create(device=cls.device, process_tag="Q")
        cls.intended = ISISInstance.objects.create(device=cls.device, process_tag="P")
        cls.native = ISISInterface.objects.create(interface=cls.interface, address_family="ipv4", instance=cls.current)
        with without_commit_drain():
            cls.overlay = acquire_overlay(
                NSOISISInterfaceState,
                management=cls.management,
                interface=cls.interface,
                af="ipv4",
                process_tag="P",
                isis_interface=cls.native,
                status="accepted",
            )

    def _repoint(self, origin):
        from netbox_routing.models import ISISInterface

        candidate = ISISInterface.objects.get(pk=self.native.pk)
        candidate.instance = self.intended
        _plan_save(candidate, ("instance",), origin=origin)

    def test_a_native_effect_without_a_value_source_is_refused(self):
        with self.assertRaisesMessage(NativeWriteRefused, "declares no value source"), _system_scope() as run:
            run(lambda: self._repoint(None))

    def test_owned_intent_is_refused(self):
        with self.assertRaises(OwnedIntentRefused), _system_scope() as run:
            run(lambda: self._repoint(OwnedIntent("netbox_nso_plugin.nsoisisinterfacestate", self.overlay.pk)))

    def test_a_mislabeled_observed_repoint_of_an_owned_anchor_is_refused(self):
        """Anchored rule: the owned interface's instance stays put whatever the declared origin."""
        with self.assertRaises(OwnedIntentRefused), _system_scope() as run:
            run(lambda: self._repoint(Observed("isis")))
        self.native.refresh_from_db()
        self.assertEqual(self.native.instance_id, self.current.pk)

    def test_an_attname_does_not_hide_a_mapped_field(self):
        """The anchored rule reads instance_id as the mapped field instance, for a save and a set update."""
        from netbox_routing.models import ISISInterface

        def save_by_attname():
            candidate = ISISInterface.objects.get(pk=self.native.pk)
            candidate.instance_id = self.intended.pk
            effect = native.active_native_writer().save(
                candidate, update_fields=("instance_id",), created=False, origin=Observed("isis")
            )
            with effect:
                candidate.save(update_fields=("instance_id",))

        def update_by_attname():
            values = {"instance_id": self.intended.pk}
            with native.active_native_writer().set_update(
                ISISInterface, [self.native.pk], values, origin=Observed("isis")
            ):
                ISISInterface.objects.filter(pk=self.native.pk).update(**values)

        for write in (save_by_attname, update_by_attname):
            with self.subTest(write=write.__name__), self.assertRaises(OwnedIntentRefused), _system_scope() as run:
                run(write)
        self.native.refresh_from_db()
        self.assertEqual(self.native.instance_id, self.current.pk)

    def test_an_observed_dependency_create_succeeds_beside_an_owned_anchor(self):
        """Dependency rule: an observed process with the intended natural key is still created."""
        from netbox_routing.models import ISISInstance

        self.intended.delete()
        created = ISISInstance(device=self.device, process_tag="P")

        def create():
            from netbox_nso_plugin.renderer_writer import (
                RendererMutationPlan,
                planned_save,
                renderer_mirror_writes,
                renderer_writes,
            )

            save = planned_save(
                created, force_insert=True, natural_key=("device", "process_tag"), origin=Observed("isis")
            )
            plan = RendererMutationPlan.build(saves=(save,))
            with (renderer_writes if plan.changes_content else renderer_mirror_writes)(plan) as writer:
                writer.save(created, force_insert=True)

        with _system_scope() as run:
            run(create)
        self.assertTrue(ISISInstance.objects.filter(device=self.device, process_tag="P").exists())

    def test_the_unowned_scope_of_the_same_interface_takes_its_facts(self):
        """Owned BFD and unowned switchport on one interface: the switchport fields are written."""
        with without_commit_drain():
            acquire_overlay(
                NSOBFDInterfaceState,
                management=self.management,
                interface=self.other,
                min_tx=300,
                min_rx=300,
                multiplier=3,
                status="accepted",
            )
        candidate = Interface.objects.get(pk=self.other.pk)
        candidate.mode = "access"
        with _system_scope() as run:
            run(lambda: _plan_save(candidate, ("mode",), origin=Observed("switchport")))
        self.other.refresh_from_db()
        self.assertEqual(self.other.mode, "access")

    def test_the_owned_switchport_scope_refuses_its_fields(self):
        Interface.objects.filter(pk=self.other.pk).update(mode="access")
        with without_commit_drain():
            acquire_overlay(
                NSOSwitchportState, management=self.management, interface=self.other, mode="access", status="accepted"
            )
        candidate = Interface.objects.get(pk=self.other.pk)
        candidate.mode = "tagged"
        with self.assertRaises(OwnedIntentRefused), _system_scope() as run:
            run(lambda: _plan_save(candidate, ("mode",), origin=Observed("switchport")))
        self.other.refresh_from_db()
        self.assertEqual(self.other.mode, "access")

    def test_an_observed_save_cannot_clear_an_owned_relation(self):
        """NetBox clears tagged VLANs on a non-tagged interface's save: C14 guards that relation change."""
        from ipam.models import VLAN

        vlan = VLAN.objects.create(vid=3005, name="nw-owned-tagged")
        Interface.objects.filter(pk=self.other.pk).update(mode="access")
        Interface.tagged_vlans.through.objects.create(interface_id=self.other.pk, vlan_id=vlan.pk)
        with without_commit_drain():
            acquire_overlay(
                NSOSwitchportState, management=self.management, interface=self.other, mode="access", status="accepted"
            )
        candidate = Interface.objects.get(pk=self.other.pk)
        candidate.description = "observed"
        with self.assertRaises(OwnedIntentRefused), _system_scope() as run:
            run(lambda: _plan_save(candidate, ("description",), origin=Observed("interface")))
        self.assertEqual(list(self.other.tagged_vlans.all()), [vlan])

    def test_a_direct_write_in_a_system_entry_needs_a_permit(self):
        from dcim.models import Site

        with self.assertRaises(NativeWriteRefused), _system_scope() as run:
            run(lambda: Site.objects.create(name="nw-direct", slug="nw-direct"))
        self.assertFalse(Site.objects.filter(slug="nw-direct").exists())
