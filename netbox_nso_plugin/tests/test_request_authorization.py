# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Endpoint authorization (#1709 increment I1).

Every plugin route enforces a declared policy. A target outside the user's restriction is
404 before any database write or adapter call; a missing model permission is 403. Read
and adapter-action endpoints cannot write native rows. The tests use real NetBox
ObjectPermission rows, real requests and the real ORM; only the adapter transport is faked.
"""

import importlib
import json
import pkgutil
import re
import sys
from contextlib import contextmanager
from unittest.mock import patch
from uuid import uuid4

import requests
from core.models import ObjectType
from dcim.models import Device, Interface, Site
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.db import connection, transaction
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import path, resolve, reverse
from django.views import View
from users.models import ObjectPermission

import netbox_nso_plugin
from netbox_nso_plugin import native_authorization as native
from netbox_nso_plugin import request_authorization as authz
from netbox_nso_plugin.api import urls as api_urls
from netbox_nso_plugin.models import NSODeviceManagement, NSOInterfaceState, NSOStaticRouteState
from netbox_nso_plugin.native_authorization import NativeWriteRefused, authority_scope
from netbox_nso_plugin.request_authorization import (
    AUTHZ_TARGETS,
    DeviceRead,
    EndpointPolicy,
    IntegrationSync,
    Management,
    OverlayFamily,
    OverlayRow,
    missing_authz_targets,
    uncovered_routes,
    view_class_of,
)

from ._outbox_case import make_managed, without_commit_drain
from ._ownership_case import acquire_overlay
from .mixins import IntentPushResetMixin

User = get_user_model()
_DML = re.compile(r'\s*(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+"?([A-Za-z0-9_]+)"?', re.IGNORECASE)
# Request bookkeeping that is not part of the endpoint's effect.
_BOOKKEEPING_TABLES = frozenset({"django_session", "users_token"})


def _plugin_patterns():
    from netbox_nso_plugin import urls

    return [*urls.urlpatterns, *api_urls.urlpatterns]


def _device_tab_view():
    return view_class_of(resolve(reverse("dcim:device_nso", kwargs={"pk": 1})).func)


def _route_classes():
    classes = {view_class_of(pattern.callback) for pattern in _plugin_patterns()}
    return {cls for cls in classes if cls is not None} | {_device_tab_view()}


class TestRouteCensus(SimpleTestCase):
    """The URL census: every route resolves to a class that enforces a declared policy."""

    def test_every_plugin_route_enforces_a_policy(self):
        self.assertEqual(uncovered_routes(_plugin_patterns()), [])

    def test_the_device_tab_enforces_a_policy(self):
        tab = _device_tab_view()
        self.assertTrue(issubclass(tab, authz.AuthorizedGenericView))
        self.assertIsInstance(tab.policy.target, DeviceRead)

    def test_census_rejects_uncovered_routes(self):
        class PlainView(View):
            pass

        class Abstract(authz.AuthorizedView, abstract=True):
            pass

        def function_view(request):
            return None

        synthetic = [
            path("plain/", PlainView.as_view()),
            path("function/", function_view),
            path("abstract/", Abstract.as_view()),
        ]
        self.assertEqual(uncovered_routes(synthetic), ["plain/", "function/", "abstract/"])

    def test_collection_targets_are_pinned(self):
        """A page without a single target lists only through restricted querysets; review each one."""
        self.assertEqual(
            authz.collection_views(_route_classes()),
            {"NSOAPIRootView", "NSOOnboardingDashboardView", "OnboardingCandidatesView"},
        )


class TestTargetRegistryCensus(SimpleTestCase):
    """AUTHZ_TARGETS covers every overlay model, independently of the settlement registry."""

    def test_every_overlay_model_has_an_authorization_target(self):
        from netbox_nso_plugin.intent_state import OVERLAY_MODEL_RANKS

        self.assertEqual(missing_authz_targets(OVERLAY_MODEL_RANKS), [])
        self.assertIn("netbox_nso_plugin.nsobgppeertemplatestate", AUTHZ_TARGETS)

    def test_census_rejects_an_overlay_model_without_a_target(self):
        self.assertEqual(
            missing_authz_targets(["netbox_nso_plugin.nsointerfacestate", "netbox_nso_plugin.nsonewstate"]),
            ["netbox_nso_plugin.nsonewstate"],
        )

    def test_every_target_path_is_a_valid_lookup(self):
        from django.apps import apps

        for label, target_path in AUTHZ_TARGETS.items():
            with self.subTest(model=label):
                model = apps.get_model(label)
                str(
                    model.objects.filter(
                        **{f"{target_path.management}__in": NSODeviceManagement.objects.filter(pk=-1)}
                    ).query
                )
                str(model.objects.filter(**{f"{target_path.device}__in": Device.objects.filter(pk=-1)}).query)


class TestImportTimeCheck(SimpleTestCase):
    """A class that skips the policy fails when Python defines it."""

    def test_a_concrete_view_without_a_policy_is_rejected(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "declares no endpoint policy"):

            class NoPolicy(authz.AuthorizedView):
                def get(self, request):
                    return None

    def test_an_inherited_policy_does_not_count(self):
        class Parent(authz.AuthorizedView):
            policy = EndpointPolicy(DeviceRead(), "read")

        with self.assertRaisesMessage(ImproperlyConfigured, "declares no endpoint policy"):

            class Child(Parent):
                pass

    def test_overriding_dispatch_is_rejected(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "overrides dispatch()"):

            class Override(authz.AuthorizedView):
                policy = EndpointPolicy(DeviceRead(), "read")

                def dispatch(self, request, *args, **kwargs):
                    return None

    def test_a_mixin_with_dispatch_before_the_enforcer_is_rejected(self):
        class Bypass:
            def dispatch(self, request, *args, **kwargs):
                return None

        with self.assertRaisesMessage(ImproperlyConfigured, "overrides dispatch()"):

            class Mixed(Bypass, authz.AuthorizedView):
                policy = EndpointPolicy(DeviceRead(), "read")

    def test_a_drf_view_overriding_initial_is_rejected(self):
        from rest_framework.views import APIView

        with self.assertRaisesMessage(ImproperlyConfigured, "overrides initial()"):

            class Api(authz.AuthorizedAPIView, APIView):
                policy = EndpointPolicy(DeviceRead(), "read")

                def initial(self, request, *args, **kwargs):
                    return None

    def test_netbox_generic_target_needs_a_netbox_generic_view(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "not a NetBox generic view"):

            class Fake(authz.AuthorizedView):
                policy = authz.generic_policy()

    def test_a_per_method_policy_must_cover_every_handler(self):
        from types import MappingProxyType

        with self.assertRaisesMessage(ImproperlyConfigured, "has no policy for post"):

            class Partial(authz.AuthorizedView):
                policy = MappingProxyType({"get": EndpointPolicy(DeviceRead(), "read")})

                def get(self, request):
                    return None

                def post(self, request):
                    return None


class TestSystemEntryCensus(SimpleTestCase):
    """Only the allowlisted device-fact reconciliation entries may enter the system scope."""

    def test_system_entries_are_the_allowlist(self):
        for module in pkgutil.walk_packages(netbox_nso_plugin.__path__, "netbox_nso_plugin."):
            if ".tests" not in module.name and ".migrations" not in module.name:
                importlib.import_module(module.name)
        self.assertEqual(
            native.SYSTEM_ENTRIES,
            {
                "netbox_nso_plugin.reconcile.reconcile_category": "refresh",
                "netbox_nso_plugin.reconcile.enqueue_device_reconcile": "device reconcile",
            },
        )


class TestNoWriteScope(TestCase):
    """Read and adapter-action endpoints refuse native writes; system entries keep them."""

    def assertRefused(self, write):
        # Django marks the enclosing atomic block for rollback on a failed save: isolate each refusal.
        with self.assertRaises(NativeWriteRefused), transaction.atomic():
            write()

    def test_read_scope_refuses_native_writes_of_every_kind(self):
        site = Site.objects.create(name="authz-site", slug="authz-site")
        for effects in ("read", "adapter_action"):
            with self.subTest(effects=effects), authority_scope(effects):
                self.assertRefused(lambda: Site.objects.create(name="authz-new", slug="authz-new"))
                self.assertRefused(lambda: Site.objects.filter(pk=site.pk).update(name="authz-renamed"))
                self.assertRefused(lambda: Site.objects.filter(pk=site.pk).delete())
        site.refresh_from_db()
        self.assertEqual(site.name, "authz-site")
        self.assertFalse(Site.objects.filter(slug="authz-new").exists())

    def test_read_scope_allows_plugin_rows(self):
        from netbox_nso_plugin.models import NSOProvisionTombstone

        with authority_scope("read"):
            row = NSOProvisionTombstone.objects.create(
                netbox_device_id=1, nso_instance="authz", nso_device_name="authz", canonical_request={}
            )
            NSOProvisionTombstone.objects.filter(pk=row.pk).update(adapter_job_id="17")
        row.refresh_from_db()
        self.assertEqual(row.adapter_job_id, "17")

    def test_read_scope_refuses_tag_assignments(self):
        from extras.models import Tag

        site = Site.objects.create(name="authz-tagged", slug="authz-tagged")
        tag = Tag.objects.create(name="authz-tag", slug="authz-tag")
        site.tags.add(tag)
        with authority_scope("read"):
            self.assertRefused(lambda: site.tags.clear())
            self.assertRefused(lambda: site.tags.add(Tag.objects.create(name="authz-two", slug="authz-two")))
        self.assertEqual(list(site.tags.all()), [tag])

    def test_read_scope_refuses_native_rows_without_change_logging(self):
        """Native means every table outside the plugin, change-logged or not, except NetBox bookkeeping."""
        from extras.models import Bookmark
        from netbox_routing.models import RouteMapEntrySetCommunity

        user = User.objects.create_user(username=f"authz-bookmark-{uuid4().hex[:6]}")
        site = Site.objects.create(name="authz-bookmarked", slug="authz-bookmarked")
        with authority_scope("read"):
            self.assertRefused(lambda: Bookmark.objects.create(object=site, user=user))
        self.assertFalse(Bookmark.objects.filter(user=user).exists())
        self.assertIn(RouteMapEntrySetCommunity._meta.db_table, native.native_tables())
        self.assertIn(RouteMapEntrySetCommunity.communities.through._meta.db_table, native.native_tables())
        self.assertNotIn(NSOInterfaceState._meta.db_table, native.native_tables())

    def test_native_mutation_scope_needs_its_operator(self):
        with self.assertRaises(NativeWriteRefused), authority_scope("native_mutation"):
            pass

    def test_a_system_entry_inside_a_read_scope_writes_through_the_native_writer(self):
        from netbox_nso_plugin.native_authorization import Observed
        from netbox_nso_plugin.renderer_writer import (
            RendererMutationPlan,
            planned_save,
            renderer_mirror_writes,
            renderer_writes,
        )

        from ._outbox_case import make_device

        interface = Interface.objects.create(device=make_device("authz-system"), name="et-0/0/1", type="virtual")
        interface.description = "observed"
        plan = RendererMutationPlan.build(
            saves=(planned_save(interface, update_fields=("description",), origin=Observed("interface")),)
        )

        def write():
            with (renderer_writes if plan.changes_content else renderer_mirror_writes)(plan) as writer:
                writer.save(interface, update_fields=("description",))

        entry = native.system_entry("test")(write)
        self.addCleanup(native.SYSTEM_ENTRIES.pop, f"{entry.__module__}.{entry.__qualname__}")
        with authority_scope("read"):
            entry()
            self.assertRefused(lambda: Site.objects.create(name="authz-after", slug="authz-after"))
        interface.refresh_from_db()
        self.assertEqual(interface.description, "observed")


class _CountingSession(requests.Session):
    """The adapter transport, counted. Every call fails fast like an unreachable adapter."""

    calls: list = []  # noqa: RUF012 (reset per test in setUp)

    def request(self, *args, **kwargs):
        type(self).calls.append(args)
        raise requests.exceptions.ConnectionError("adapter transport counted in tests")


def _grant(user, actions, model, constraints=None):
    permission = ObjectPermission.objects.create(
        name=f"{'-'.join(actions)}-{model._meta.model_name}-{uuid4().hex[:6]}",
        actions=list(actions),
        constraints=constraints,
    )
    permission.object_types.add(ObjectType.objects.get_for_model(model))
    permission.users.add(user)
    return permission


def _overlay_rows(device, management):
    """Create one imported row of every overlay model on *device*."""
    from django.apps import apps
    from django.contrib.contenttypes.models import ContentType
    from ipam.models import VLAN
    from netbox_routing.models import PrefixList

    from netbox_nso_plugin.intent_state import OVERLAY_MODEL_RANKS
    from netbox_nso_plugin.vlan_reconciler import _device_vlan_group

    from ._settlement_case import _route

    lag = Interface.objects.create(device=device, name="Port-channel9", type="lag")
    vlan = VLAN.objects.create(group=_device_vlan_group(device), vid=909, name="authz-vlan")
    policy = PrefixList.objects.create(name=f"authz-policy-{device.pk}")
    values = {
        "nsointerfacestate": {"attribute": "description"},
        "nsointerfaceipstate": {"address": "198.18.9.1/24"},
        "nsosnmpcommunitystate": {"community_hash": "b" * 64},
        "nsosnmpv3userstate": {"username": "authz-user"},
        "nsosnmphoststate": {"address": "198.18.9.3"},
        "nsologginghoststate": {"address": "198.18.9.4"},
        "nsovlanstate": {"vlan": vlan},
        "nsostaticroutestate": {"static_route": _route("198.18.90.0/24", "198.18.9.1", devices=[device])},
        "nsol2sapstate": {"service_name": "authz-service", "sap_id": "authz-sap"},
        "nsoisisinstancestate": {"process_tag": "authz"},
        "nsoisisinterfacestate": {"process_tag": "authz", "af": "ipv4"},
        "nsoisisflexalgostate": {"process_tag": "authz", "algo_id": 130},
        "nsobgprouterstate": {"asn_str": "64512"},
        "nsobgppeerstate": {"asn_str": "64512", "peer_address_str": "198.18.9.9"},
        "nsobgppeertemplatestate": {"template_name": "authz-template"},
        "nsoroutepolicystate": {
            "family": "prefix_list",
            "object_name": policy.name,
            "content_type": ContentType.objects.get_for_model(policy),
            "object_id": policy.pk,
        },
        "nsoospfinstancestate": {"process_id": "9"},
        "nsoospfinterfacestate": {"process_id": "9"},
        "nsoredistributionstate": {"dest_protocol": "bgp", "source_protocol": "static"},
        "nsolacpbundlestate": {"interface": lag, "lag_id": 9},
    }
    rows = {}
    with without_commit_drain():
        for index, label in enumerate(OVERLAY_MODEL_RANKS):
            model = apps.get_model(label)
            name = model._meta.model_name
            data = {"status": "imported", **values.get(name, {})}
            fields = {field.name for field in model._meta.fields}
            if "management" in fields:
                data["management"] = management
            if "interface" in fields and "interface" not in data:
                data["interface"] = Interface.objects.create(
                    device=device, name=f"Ethernet9/{index}", type="1000base-t"
                )
            rows[model] = acquire_overlay(model, **data)
    return rows


class TestExcludedTargetCensus(IntentPushResetMixin, TestCase):
    """Behaviour census over every device-scoped route (UI and API).

    The user may act on management A only. Every route that names device B, management B
    or an overlay row on device B answers 404, writes nothing and never reaches the adapter.
    A user without the model permissions gets 403 on the same requests.
    """

    #: Target kinds this census does not cover; each has its own tests below.
    _OTHER_KINDS = (
        authz.NetBoxGeneric,
        authz.Collection,
        authz.Instance,
        authz.Onboarding,
        authz.ApiOnboarding,
        authz.OwnerRow,
        authz.IntegrationProvisionAttempt,
    )

    @classmethod
    def setUpTestData(cls):
        cls.device_a, cls.management_a = make_managed("authz-a", 17091)
        cls.device_b, cls.management_b = make_managed("authz-b", 17092)
        cls.rows_b = _overlay_rows(cls.device_b, cls.management_b)

    def setUp(self):
        super().setUp()
        _CountingSession.calls = []
        transport = patch("netbox_nso_plugin.adapter_client.requests.Session", _CountingSession)
        transport.start()
        self.addCleanup(transport.stop)

    def _cases(self):
        """Yield (route name, method, url, kwargs, policy) for every device-scoped route against B."""
        for pattern in _plugin_patterns():
            cls = view_class_of(pattern.callback)
            policy = cls.policy
            by_method = (
                {method: policy for method in ("get", "post") if callable(getattr(cls, method, None))}
                if isinstance(policy, EndpointPolicy)
                else {method: value for method, value in policy.items() if method in ("get", "post")}
            )
            for method, method_policy in by_method.items():
                target = method_policy.target
                if isinstance(target, self._OTHER_KINDS):
                    continue
                for kwargs in self._kwargs_for(target, pattern):
                    yield pattern, method, kwargs, method_policy

    def _kwargs_for(self, target, pattern):
        names = set(pattern.pattern.converters)
        base = {"key": "static", "action": "sync"}
        if isinstance(target, DeviceRead):
            yield {**{n: base[n] for n in names if n in base}, target.kwarg: self.device_b.pk}
        elif isinstance(target, Management):
            value = self.device_b.pk if target.by == "device" else self.management_b.pk
            yield {**{n: base[n] for n in names if n in base}, target.kwarg: value}
        elif isinstance(target, OverlayRow):
            yield {target.kwarg: self.rows_b[target.model].pk}
        elif isinstance(target, OverlayFamily):
            for key, model in target.families.items():
                yield {"key": key, "pk": self.rows_b[model].pk}
        elif isinstance(target, IntegrationSync):
            yield {}
        else:
            self.fail(f"the census has no case for target kind {type(target).__name__}")

    def _request(self, client, pattern, method, kwargs, policy, *, api_header=None):
        url = reverse(
            f"plugins{'-api' if api_header else ''}:netbox_nso_plugin{'-api' if api_header else ''}:{pattern.name}",
            kwargs=kwargs,
        )
        if isinstance(policy.target, IntegrationSync):
            return client.post(url, {"netbox_device_id": self.device_b.pk}, format="json", **api_header)
        if api_header is not None:
            return getattr(client, method)(url, format="json", **api_header)
        return getattr(client, method)(url)

    def _client_for(self, user, pattern):
        from rest_framework.test import APIClient
        from users.constants import TOKEN_PREFIX
        from users.models import Token

        if pattern in api_urls.urlpatterns:
            token = Token.objects.create(user=user)
            return APIClient(), {"HTTP_AUTHORIZATION": f"Bearer {TOKEN_PREFIX}{token.key}.{token.token}"}
        self.client.force_login(user)
        return self.client, None

    def _operator(self):
        """Grant every model permission the routes need; management actions only on A."""
        user = User.objects.create_user(username=f"authz-operator-{uuid4().hex[:6]}")
        _grant(user, ("view", "change", "add", "sync"), NSODeviceManagement, {"pk": self.management_a.pk})
        needed = set()
        for _pattern, _method, _kwargs, policy in self._cases():
            needed.update(policy.target.permissions())
            needed.update(policy.permissions)
        needed.add("ipam.change_vlan")
        from django.apps import apps
        from utilities.permissions import resolve_permission

        for permission in sorted(needed):
            app_label, action, model_name = resolve_permission(permission)
            model = apps.get_model(app_label, model_name)
            if model is not NSODeviceManagement:
                _grant(user, (action,), model)
        return user

    @contextmanager
    def _no_effect(self, label):
        before = len(_CountingSession.calls)
        with CaptureQueriesContext(connection) as queries:
            yield
        writes = {
            match.group(1)
            for query in queries.captured_queries
            if (match := _DML.match(query["sql"])) is not None and match.group(1) not in _BOOKKEEPING_TABLES
        }
        self.assertEqual(writes, set(), f"{label} wrote rows")
        self.assertEqual(len(_CountingSession.calls), before, f"{label} reached the adapter")

    def test_census_covers_device_scoped_routes(self):
        names = {pattern.name for pattern, *_rest in self._cases()}
        for expected in (
            "device_nso_category",
            "nsodevicemanagement_action",
            "routing_accept_static_route",
            "overlay_field_edit",
            "routing_bulk_accept_static_routes",
            "generation_retry",
            "sync_complete",
        ):
            self.assertIn(expected, names)

    def test_an_excluded_target_is_404_with_no_write_and_no_adapter_call(self):
        user = self._operator()
        for pattern, method, kwargs, policy in self._cases():
            label = f"{method.upper()} {pattern.name} {kwargs}"
            client, header = self._client_for(user, pattern)
            with self.subTest(route=label), self._no_effect(label):
                response = self._request(client, pattern, method, kwargs, policy, api_header=header)
                self.assertEqual(response.status_code, 404, label)

    def test_a_missing_model_permission_is_403_with_no_adapter_call(self):
        user = User.objects.create_user(username=f"authz-nobody-{uuid4().hex[:6]}")
        for pattern, method, kwargs, policy in self._cases():
            label = f"{method.upper()} {pattern.name} {kwargs}"
            client, header = self._client_for(user, pattern)
            with self.subTest(route=label), self._no_effect(label):
                response = self._request(client, pattern, method, kwargs, policy, api_header=header)
                self.assertEqual(response.status_code, 403, label)

    def test_the_permitted_management_still_works(self):
        """A constrained holder keeps today's behaviour on the management the constraint allows."""
        user = self._operator()
        self.client.force_login(user)
        counts = self.client.get(
            reverse("plugins:netbox_nso_plugin:device_nso_category_counts", kwargs={"device_pk": self.device_a.pk})
        )
        self.assertEqual(counts.status_code, 200)
        action = self.client.post(
            reverse(
                "plugins:netbox_nso_plugin:nsodevicemanagement_action",
                kwargs={"pk": self.management_a.pk, "action": "detect-drift"},
            )
        )
        self.assertEqual(action.status_code, 302)
        self.assertEqual(len(_CountingSession.calls), 1)
        excluded = self.rows_b[NSOStaticRouteState]
        response = self.client.post(
            reverse("plugins:netbox_nso_plugin:routing_accept_static_route", kwargs={"pk": excluded.pk})
        )
        self.assertEqual(response.status_code, 404)
        excluded.refresh_from_db()
        self.assertEqual(excluded.status, "imported")


class TestTargetKinds(IntentPushResetMixin, TestCase):
    """The target kinds the route census leaves out, each with real constrained permissions."""

    @classmethod
    def setUpTestData(cls):
        cls.device_a, cls.management_a = make_managed("authz-k-a", 17093)
        cls.device_b, cls.management_b = make_managed("authz-k-b", 17094)

    def setUp(self):
        super().setUp()
        _CountingSession.calls = []
        transport = patch("netbox_nso_plugin.adapter_client.requests.Session", _CountingSession)
        transport.start()
        self.addCleanup(transport.stop)
        self.user = User.objects.create_user(username=f"authz-kinds-{uuid4().hex[:6]}")
        self.client.force_login(self.user)

    def _api(self):
        from rest_framework.test import APIClient
        from users.constants import TOKEN_PREFIX
        from users.models import Token

        token = Token.objects.create(user=self.user)
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {TOKEN_PREFIX}{token.key}.{token.token}")
        return api

    def test_the_device_tab_hides_an_excluded_management(self):
        _grant(self.user, ("view",), Device)
        _grant(self.user, ("view",), NSODeviceManagement, {"pk": self.management_a.pk})
        excluded = self.client.get(reverse("dcim:device_nso", kwargs={"pk": self.device_b.pk}))
        self.assertEqual(excluded.status_code, 404)
        self.assertEqual(_CountingSession.calls, [])

    def test_sync_complete_needs_the_integration_permission_and_the_target(self):
        api = self._api()
        url = reverse("plugins-api:netbox_nso_plugin-api:sync_complete")
        with patch("netbox_nso_plugin.reconcile.enqueue_device_reconcile") as enqueue:
            denied = api.post(url, {"netbox_device_id": self.device_a.pk}, format="json")
            _grant(self.user, ("sync",), NSODeviceManagement, {"pk": self.management_a.pk})
            api = self._api()
            excluded = api.post(url, {"netbox_device_id": self.device_b.pk}, format="json")
            allowed = api.post(url, {"adapter_device_id": self.management_a.adapter_device_id}, format="json")
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(excluded.status_code, 404)
        self.assertEqual(allowed.status_code, 202)
        enqueue.assert_called_once_with(self.device_a.pk)

    def test_provision_evidence_is_bound_to_the_permitted_attempt(self):
        from netbox_nso_plugin.models import NSOProvisionTombstone

        def tombstone(device_id, name):
            return NSOProvisionTombstone.objects.create(
                netbox_device_id=device_id, nso_instance="authz-inst", nso_device_name=name, canonical_request={}
            )

        permitted = tombstone(self.device_a.pk, "authz-permitted")
        excluded = tombstone(self.device_b.pk, "authz-excluded")
        # The attempt outlives its device: the scalar id names no row, and the evidence still lands.
        orphan = tombstone(10**9, "authz-orphan")
        _grant(
            self.user,
            ("change",),
            NSOProvisionTombstone,
            {"nso_device_name__in": ["authz-permitted", "authz-orphan"]},
        )
        api = self._api()
        url = reverse("plugins-api:netbox_nso_plugin-api:provision_complete")
        with patch("netbox_nso_plugin.reconcile.enqueue_provision_tombstone_sweep") as enqueue:
            responses = {
                row.nso_device_name: api.post(
                    url, {"provision_attempt_id": str(row.provision_attempt_id), "status": "failed"}, format="json"
                ).status_code
                for row in (permitted, excluded, orphan)
            }
        self.assertEqual(responses, {"authz-permitted": 202, "authz-excluded": 404, "authz-orphan": 202})
        excluded.refresh_from_db()
        self.assertEqual(excluded.state, "open")
        orphan.refresh_from_db()
        self.assertEqual(orphan.state, "terminal")
        self.assertEqual(enqueue.call_count, 2)

    def _view_device_a_only(self):
        _grant(self.user, ("view",), Device, {"pk": self.device_a.pk})
        _grant(self.user, ("view",), NSODeviceManagement)

    def test_a_category_refresh_needs_change_authority(self):
        """A refresh enters the system scope, so its trigger needs change on the management (D5)."""
        _grant(self.user, ("view",), Device)
        _grant(self.user, ("view",), NSODeviceManagement)
        category = reverse(
            "plugins:netbox_nso_plugin:device_nso_category", kwargs={"pk": self.device_b.pk, "key": "vlan"}
        )
        self.assertEqual(self.client.get(category, {"refresh": "1"}).status_code, 403)
        _grant(self.user, ("change",), NSODeviceManagement, {"pk": self.management_a.pk})
        self.assertEqual(self.client.get(category, {"refresh": "1"}).status_code, 404)
        expand = reverse(
            "plugins:netbox_nso_plugin:device_nso_category", kwargs={"pk": self.device_b.pk, "key": "lacp"}
        )
        self.assertEqual(self.client.get(expand).status_code, 200)
        self.assertEqual(_CountingSession.calls, [])

    def test_the_interface_grid_hides_an_excluded_cable_peer(self):
        from dcim.models import Cable, CableTermination

        from netbox_nso_plugin.models import NSOInterfaceIPState

        local = Interface.objects.create(device=self.device_a, name="Ethernet41", type="1000base-t")
        remote = Interface.objects.create(device=self.device_b, name="Ethernet42", type="1000base-t")
        cable = Cable.objects.create(status="connected")
        CableTermination.objects.create(cable=cable, cable_end="A", termination=local)
        CableTermination.objects.create(cable=cable, cable_end="B", termination=remote)
        with without_commit_drain():
            acquire_overlay(NSOInterfaceIPState, interface=local, address="198.18.40.1/30", status="imported")
            acquire_overlay(NSOInterfaceIPState, interface=remote, address="198.18.40.2/30", status="imported")
        url = reverse(
            "plugins:netbox_nso_plugin:device_nso_category", kwargs={"pk": self.device_a.pk, "key": "interface"}
        )
        self.client.force_login(User.objects.create_superuser(username=f"authz-peer-admin-{uuid4().hex[:6]}"))
        self.assertIn(self.device_b.name, self.client.get(url, {"format": "json"}).content.decode())
        self.client.force_login(self.user)
        self._view_device_a_only()
        _grant(self.user, ("view",), Interface)
        response = self.client.get(url, {"format": "json"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(self.device_b.name, response.content.decode())
        self.assertNotIn("198.18.40.2", response.content.decode())

    def test_owner_state_api_needs_the_rows_device(self):
        with without_commit_drain():
            interface = Interface.objects.create(device=self.device_b, name="Ethernet32", type="1000base-t")
            row = acquire_overlay(NSOInterfaceState, interface=interface, attribute="description")
        _grant(self.user, ("view",), NSOInterfaceState)
        self._view_device_a_only()
        url = reverse("plugins-api:netbox_nso_plugin-api:owner-state-detail", args=(row._meta.model_name, row.pk))
        self.assertEqual(self._api().get(url).status_code, 404)

    def test_sync_complete_with_both_identities(self):
        _grant(self.user, ("sync",), NSODeviceManagement, {"pk": self.management_a.pk})
        api = self._api()
        url = reverse("plugins-api:netbox_nso_plugin-api:sync_complete")
        with patch("netbox_nso_plugin.reconcile.enqueue_device_reconcile") as enqueue:
            excluded = api.post(
                url,
                {"netbox_device_id": self.device_b.pk, "adapter_device_id": self.management_b.adapter_device_id},
                format="json",
            )
            mismatched = api.post(
                url,
                {"netbox_device_id": self.device_a.pk, "adapter_device_id": self.management_b.adapter_device_id},
                format="json",
            )
        self.assertEqual((excluded.status_code, mismatched.status_code), (404, 400))
        enqueue.assert_not_called()

    def test_an_ip_edit_does_not_clear_an_excluded_inverse_peer(self):
        from ipam.models import IPAddress

        from netbox_nso_plugin.models import NSOInterfaceIPState

        from ._ownership_case import save_overlay_fixture

        with without_commit_drain():
            local = acquire_overlay(
                NSOInterfaceIPState,
                interface=Interface.objects.create(device=self.device_a, name="Ethernet51", type="1000base-t"),
                address="198.18.50.1/30",
                auto_assigned=True,
                status="accepted",
            )
            remote = acquire_overlay(
                NSOInterfaceIPState,
                interface=Interface.objects.create(device=self.device_b, name="Ethernet52", type="1000base-t"),
                address="198.18.50.2/30",
                auto_assigned=True,
                peer_state=local,
                status="accepted",
            )
            local.peer_state = remote
            save_overlay_fixture(local, update_fields=("peer_state",))
        _grant(self.user, ("change",), NSODeviceManagement, {"pk": self.management_a.pk})
        _grant(self.user, ("add", "change"), IPAddress)
        response = self.client.post(
            reverse("plugins:netbox_nso_plugin:nsointerfaceipstate_edit", kwargs={"pk": local.pk}),
            {"address": "198.18.50.5/30"},
        )
        self.assertEqual(response.status_code, 404)
        remote.refresh_from_db()
        local.refresh_from_db()
        self.assertEqual((remote.peer_state_id, local.address), (local.pk, "198.18.50.1/30"))

    def _unmanaged_pair(self, name):
        from netbox_nso_plugin.models import NSOInterfaceIPState

        from ._ownership_case import save_overlay_fixture

        unmanaged = Device.objects.create(
            name=name, device_type=self.device_a.device_type, role=self.device_a.role, site=self.device_a.site
        )
        with without_commit_drain():
            local = acquire_overlay(
                NSOInterfaceIPState,
                interface=Interface.objects.create(device=self.device_a, name=f"{name}-a", type="1000base-t"),
                address="198.18.57.1/30",
                auto_assigned=True,
                status="accepted",
            )
            remote = acquire_overlay(
                NSOInterfaceIPState,
                interface=Interface.objects.create(device=unmanaged, name=f"{name}-b", type="1000base-t"),
                address="198.18.57.2/30",
                auto_assigned=True,
                peer_state=local,
                status="accepted",
            )
            local.peer_state = remote
            save_overlay_fixture(local, update_fields=("peer_state",))
        return unmanaged, local, remote

    def test_writing_an_unmanaged_peer_row_needs_change_on_its_device(self):
        """A row on an unmanaged device has no management authority: a write needs change on the device."""
        from ipam.models import IPAddress

        unmanaged, local, remote = self._unmanaged_pair("authz-peer-write")
        _grant(self.user, ("change",), NSODeviceManagement, {"pk": self.management_a.pk})
        _grant(self.user, ("add", "change"), IPAddress)
        _grant(self.user, ("view",), Device)
        url = reverse("plugins:netbox_nso_plugin:nsointerfaceipstate_edit", kwargs={"pk": local.pk})
        for granted, expected in ((False, 403), (True, 404)):
            if granted:
                _grant(self.user, ("change",), Device, {"pk": self.device_a.pk})
            with self.subTest(change_device=granted), CaptureQueriesContext(connection) as queries:
                response = self.client.post(url, {"address": "198.18.57.5/30"})
            self.assertEqual(response.status_code, expected)
            writes = {m.group(1) for q in queries.captured_queries if (m := _DML.match(q["sql"])) is not None}
            self.assertEqual(writes - _BOOKKEEPING_TABLES, set())
        remote.refresh_from_db()
        local.refresh_from_db()
        self.assertEqual((remote.peer_state_id, local.address), (local.pk, "198.18.57.1/30"))
        self.assertTrue(Device.objects.filter(pk=unmanaged.pk).exists())

    def test_an_unchanged_peer_prefill_does_not_need_peer_authority(self):
        """The edit form submits the peer address unchanged; only a written peer is a second target (A4)."""
        from dcim.models import Cable, CableTermination
        from ipam.models import IPAddress

        from netbox_nso_plugin.models import NSOInterfaceIPState

        unmanaged = Device.objects.create(
            name="authz-peer-prefill",
            device_type=self.device_a.device_type,
            role=self.device_a.role,
            site=self.device_a.site,
        )
        local_iface = Interface.objects.create(device=self.device_a, name="Ethernet61", type="1000base-t")
        remote_iface = Interface.objects.create(device=unmanaged, name="Ethernet62", type="1000base-t")
        cable = Cable.objects.create(status="connected")
        CableTermination.objects.create(cable=cable, cable_end="A", termination=local_iface)
        CableTermination.objects.create(cable=cable, cable_end="B", termination=remote_iface)
        with without_commit_drain():
            local = acquire_overlay(
                NSOInterfaceIPState, interface=local_iface, address="198.18.61.1/30", status="imported"
            )
            remote = acquire_overlay(
                NSOInterfaceIPState, interface=remote_iface, address="198.18.61.2/30", status="imported"
            )
        _grant(self.user, ("change",), NSODeviceManagement, {"pk": self.management_a.pk})
        _grant(self.user, ("add", "change"), IPAddress)
        _grant(self.user, ("view",), Device)
        response = self.client.post(
            reverse("plugins:netbox_nso_plugin:nsointerfaceipstate_edit", kwargs={"pk": local.pk}),
            {"address": "198.18.61.5/30", "peer_address": "198.18.61.2/30"},
        )
        self.assertEqual(response.status_code, 200)
        local.refresh_from_db()
        remote.refresh_from_db()
        self.assertEqual((local.address, remote.address), ("198.18.61.5/30", "198.18.61.2/30"))
        changed = self.client.post(
            reverse("plugins:netbox_nso_plugin:nsointerfaceipstate_edit", kwargs={"pk": local.pk}),
            {"address": "198.18.61.5/30", "peer_address": "198.18.61.6/30"},
        )
        self.assertEqual(changed.status_code, 403)
        remote.refresh_from_db()
        self.assertEqual(remote.address, "198.18.61.2/30")

    def test_an_unmanaged_interface_overlay_is_a_device_scoped_target(self):
        """With no management row, a primary interface overlay is authorized by its device (A4 for superusers)."""
        from netbox_nso_plugin.models import NSOInterfaceIPState

        unmanaged = Device.objects.create(
            name="authz-orphan", device_type=self.device_a.device_type, role=self.device_a.role, site=self.device_a.site
        )
        with without_commit_drain():
            row = acquire_overlay(
                NSOInterfaceIPState,
                interface=Interface.objects.create(device=unmanaged, name="Ethernet71", type="1000base-t"),
                address="198.18.71.1/30",
                status="imported",
            )
        edit = reverse("plugins:netbox_nso_plugin:nsointerfaceipstate_edit", kwargs={"pk": row.pk})
        owner = reverse("plugins-api:netbox_nso_plugin-api:owner-state-detail", args=(row._meta.model_name, row.pk))
        _grant(self.user, ("change",), NSODeviceManagement)
        _grant(self.user, ("view",), Device)
        self.assertEqual(self.client.post(edit, {"address": "198.18.71.5/30"}).status_code, 403)
        _grant(self.user, ("change",), Device, {"pk": self.device_a.pk})
        self.assertEqual(self.client.post(edit, {"address": "198.18.71.5/30"}).status_code, 404)
        row.refresh_from_db()
        self.assertEqual(row.address, "198.18.71.1/30")
        superuser = User.objects.create_superuser(username=f"authz-orphan-admin-{uuid4().hex[:6]}")
        self.client.force_login(superuser)
        self.assertEqual(self.client.post(edit, {"address": "198.18.71.5/30"}).status_code, 200)
        row.refresh_from_db()
        self.assertEqual(row.address, "198.18.71.5/30")
        self.user = superuser
        self.assertEqual(self._api().get(owner).status_code, 200)

    def test_an_excluded_instance_selection_is_404_not_the_default(self):
        """A mutating onboard never substitutes the default instance for an excluded selection."""
        from netbox_nso_plugin.models import NSOInstance

        unmanaged = Device.objects.create(
            name="authz-manage-me",
            device_type=self.device_a.device_type,
            role=self.device_a.role,
            site=self.device_a.site,
        )
        excluded = self.management_b.nso_instance
        default = self.management_a.nso_instance
        NSOInstance.objects.filter(pk=default.pk).update(is_default=True)
        NSOInstance.objects.exclude(pk=default.pk).update(is_default=False)
        _grant(self.user, ("add",), NSODeviceManagement)
        _grant(self.user, ("view",), Device)
        _grant(self.user, ("view",), NSOInstance, {"pk": default.pk})
        response = self.client.post(
            reverse("plugins:netbox_nso_plugin:quick_manage"),
            {"device": unmanaged.pk, "instance": excluded.adapter_instance_id, "nso_name": "authz-manage-me"},
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(NSODeviceManagement.objects.filter(device=unmanaged).exists())
        self.assertEqual(_CountingSession.calls, [])

    def test_onboarding_rejects_invalid_device_identities(self):
        previous_limit = sys.get_int_max_str_digits()
        sys.set_int_max_str_digits(4300)
        self.addCleanup(sys.set_int_max_str_digits, previous_limit)
        _grant(self.user, ("add",), NSODeviceManagement)
        _grant(self.user, ("view",), Device)
        before = list(NSODeviceManagement.objects.values())
        for route in ("onboard", "quick_manage"):
            for value in ("²", "①", "9" * 5000):
                with self.subTest(route=route, value=value if len(value) < 20 else "oversized"):
                    response = self.client.post(reverse(f"plugins:netbox_nso_plugin:{route}"), {"device": value})
                    self.assertEqual(response.status_code, 404)
                    self.assertEqual(list(NSODeviceManagement.objects.values()), before)
                    self.assertEqual(_CountingSession.calls, [])

    def test_bulk_interface_actions_ignore_invalid_ids(self):
        previous_limit = sys.get_int_max_str_digits()
        sys.set_int_max_str_digits(4300)
        self.addCleanup(sys.set_int_max_str_digits, previous_limit)
        _grant(self.user, ("change",), NSODeviceManagement)
        _grant(self.user, ("view",), Device)
        for route in ("device_auto_assign_ip", "device_provision_link_role"):
            for value in ("²", "9" * 5000):
                with self.subTest(route=route, value=value if len(value) < 20 else "oversized"):
                    url = reverse(f"plugins:netbox_nso_plugin:{route}", kwargs={"device_pk": self.device_a.pk})
                    response = self.client.post(url, {"interface_pks": value})
                    self.assertEqual(response.status_code, 302)
                    self.assertEqual(_CountingSession.calls, [])

    def test_anonymous_device_tab_honors_netbox_view_exemption(self):
        device = Device.objects.create(
            name="authz-public",
            device_type=self.device_a.device_type,
            role=self.device_a.role,
            site=self.device_a.site,
        )
        self.client.logout()
        with self.settings(LOGIN_REQUIRED=False, EXEMPT_VIEW_PERMISSIONS=["dcim.device"]):
            response = self.client.get(reverse("dcim:device_nso", kwargs={"pk": device.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(_CountingSession.calls, [])

    def test_onboarding_without_device_view_is_403(self):
        _grant(self.user, ("add",), NSODeviceManagement)
        response = self.client.post(reverse("plugins:netbox_nso_plugin:onboard"), {"device": self.device_a.pk})
        self.assertEqual(response.status_code, 403)

    def test_api_onboarding_rejects_malformed_identities_before_writes(self):
        from netbox_nso_plugin.models import NSOInstance

        _grant(self.user, ("view",), Device)
        _grant(self.user, ("add", "change"), NSODeviceManagement)
        _grant(self.user, ("view",), NSOInstance)
        api = self._api()
        url = reverse("plugins-api:netbox_nso_plugin-api:onboard")
        before = list(NSODeviceManagement.objects.values())
        invalid = [{"netbox_device_id": value} for value in ("abc", str(self.device_a.pk), True, False, 0, -1, [], {})]
        invalid += [
            {"netbox_device_id": self.device_a.pk, "instance": value}
            for value in ([], {}, 1, True, "invalid\x00instance", "\ud800")
        ]
        for body in invalid:
            with self.subTest(body=body):
                response = api.post(url, json.dumps(body), content_type="application/json")
                self.assertEqual(response.status_code, 400, response.content)
                self.assertEqual(list(NSODeviceManagement.objects.values()), before)
                self.assertEqual(_CountingSession.calls, [])

    def test_instance_selections_reject_null_characters_before_lookup(self):
        from netbox_nso_plugin.models import NSOInstance

        _grant(self.user, ("view",), Device)
        _grant(self.user, ("view", "add", "change"), NSODeviceManagement)
        _grant(self.user, ("view",), NSOInstance)
        api = self._api()
        before = list(NSODeviceManagement.objects.values())
        for route, client, method, body in (
            ("plugins:netbox_nso_plugin:onboard", self.client, "post", {"device": self.device_a.pk}),
            ("plugins:netbox_nso_plugin:quick_manage", self.client, "post", {"device": self.device_a.pk}),
            ("plugins:netbox_nso_plugin:onboarding_dashboard", self.client, "get", {}),
            ("plugins-api:netbox_nso_plugin-api:onboarding_candidates", api, "get", {}),
        ):
            with self.subTest(route=route):
                response = getattr(client, method)(reverse(route), {**body, "instance": "invalid\x00instance"})
                self.assertEqual(response.status_code, 400, response.content)
                self.assertEqual(list(NSODeviceManagement.objects.values()), before)
                self.assertEqual(_CountingSession.calls, [])

    def test_a_sorted_plugin_list_saves_the_users_ordering(self):
        from netbox_nso_plugin.models import NSOInstance
        from netbox_nso_plugin.tables import NSOInstanceTable

        _grant(self.user, ("view",), NSOInstance)
        response = self.client.get(reverse("plugins:netbox_nso_plugin:nsoinstance_list"), {"sort": "-name"})
        self.assertEqual(response.status_code, 200)
        self.user.config.refresh_from_db()
        self.assertEqual(self.user.config.get(f"tables.{NSOInstanceTable.__name__}.ordering"), ["-name"])

    def test_a_superuser_edit_still_clears_an_unmanaged_inverse_peer(self):
        """An interface overlay on an unmanaged device has no management authority; a superuser keeps the edit (A4)."""
        from netbox_nso_plugin.models import NSOInterfaceIPState

        from ._ownership_case import save_overlay_fixture

        unmanaged = Device.objects.create(
            name="authz-peer-unmanaged",
            device_type=self.device_a.device_type,
            role=self.device_a.role,
            site=self.device_a.site,
        )
        with without_commit_drain():
            local = acquire_overlay(
                NSOInterfaceIPState,
                interface=Interface.objects.create(device=self.device_a, name="Ethernet55", type="1000base-t"),
                address="198.18.55.1/30",
                auto_assigned=True,
                status="accepted",
            )
            remote = acquire_overlay(
                NSOInterfaceIPState,
                interface=Interface.objects.create(device=unmanaged, name="Ethernet56", type="1000base-t"),
                address="198.18.55.2/30",
                auto_assigned=True,
                peer_state=local,
                status="accepted",
            )
            local.peer_state = remote
            save_overlay_fixture(local, update_fields=("peer_state",))
        self.client.force_login(User.objects.create_superuser(username=f"authz-peer-super-{uuid4().hex[:6]}"))
        response = self.client.post(
            reverse("plugins:netbox_nso_plugin:nsointerfaceipstate_edit", kwargs={"pk": local.pk}),
            {"address": "198.18.55.5/30"},
        )
        self.assertEqual(response.status_code, 200)
        remote.refresh_from_db()
        self.assertIsNone(remote.peer_state_id)

    def test_the_onboarding_dashboard_lists_only_visible_managed_devices(self):
        from netbox_nso_plugin.models import NSOInstance

        _grant(self.user, ("view",), NSOInstance)
        _grant(self.user, ("view",), NSODeviceManagement)
        _grant(self.user, ("view",), Device, {"pk": self.device_a.pk})
        url = reverse("plugins:netbox_nso_plugin:onboarding_dashboard")
        response = self.client.get(url, {"instance": self.management_b.nso_instance.adapter_instance_id})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["managed"], [])

    def test_generic_routes_keep_the_netbox_model_restriction(self):
        """Generic overlay CRUD authorizes by the overlay model's own permission (design 2b): constrained is 404."""
        with without_commit_drain():
            interface = Interface.objects.create(device=self.device_b, name="Ethernet53", type="1000base-t")
            row = acquire_overlay(NSOInterfaceState, interface=interface, attribute="description")
        _grant(self.user, ("view", "delete"), NSOInterfaceState, {"interface__device_id": self.device_a.pk})
        for url in (
            reverse("plugins:netbox_nso_plugin:nsointerfacestate", kwargs={"pk": row.pk}),
            reverse("plugins:netbox_nso_plugin:nsointerfacestate_delete", kwargs={"pk": row.pk}),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)
        api = reverse("plugins-api:netbox_nso_plugin-api:nsointerfacestate-detail", kwargs={"pk": row.pk})
        self.assertEqual(self._api().get(api).status_code, 404)

    def test_a_device_read_without_management_view_is_403(self):
        _grant(self.user, ("view",), Device)
        url = reverse("plugins:netbox_nso_plugin:device_nso_jobs", kwargs={"pk": self.device_a.pk})
        self.assertEqual(self.client.get(url).status_code, 403)
        _grant(self.user, ("view",), NSODeviceManagement, {"pk": self.management_b.pk})
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_the_onboarded_tile_needs_the_management_too(self):
        from netbox_nso_plugin.models import NSOInstance
        from netbox_nso_plugin.onboarding import build_onboarding_dashboard

        _grant(self.user, ("view",), NSOInstance)
        _grant(self.user, ("view",), Device)
        _grant(self.user, ("view",), NSODeviceManagement, {"pk": self.management_a.pk})
        nso_devices = [
            {"name": name, "onboarded_netbox_device_id": device.pk}
            for name, device in (("nso-a", self.device_a), ("nso-b", self.device_b))
        ]
        with patch("netbox_nso_plugin.adapter_client.list_instance_devices", return_value=nso_devices):
            data = build_onboarding_dashboard(self.management_a.nso_instance, self.user)
        self.assertEqual([entry["netbox_device"] for entry in data["onboarded"]], [self.device_a])

    def test_settings_singletons_keep_their_model_restriction(self):
        from netbox_nso_plugin.models import AdapterConnection, NSOFailoverSettings

        connection_row = AdapterConnection.objects.create(url="http://authz-adapter")
        failover_row = NSOFailoverSettings.objects.create()
        _CountingSession.calls = []  # saving the settings row pushed it to the adapter
        _grant(self.user, ("add",), AdapterConnection)
        response = self.client.post(
            reverse("plugins:netbox_nso_plugin:adapterconnection"), {"url": "http://authz-overwrite"}
        )
        self.assertEqual(response.status_code, 403)
        connection_row.refresh_from_db()
        self.assertEqual(connection_row.url, "http://authz-adapter")
        _grant(self.user, ("change",), NSOFailoverSettings, {"pk": failover_row.pk + 1})
        self.assertEqual(self.client.get(reverse("plugins:netbox_nso_plugin:nsofailoversettings")).status_code, 404)
        self.assertEqual(_CountingSession.calls, [])

    def test_an_excluded_nso_instance_inventory_is_404(self):
        from netbox_nso_plugin.models import NSOInstance

        _grant(self.user, ("view",), NSOInstance, {"pk": self.management_a.nso_instance_id})
        excluded = self.client.get(
            reverse(
                "plugins:netbox_nso_plugin:ajax_nso_device_names",
                kwargs={"instance_pk": self.management_b.nso_instance_id},
            )
        )
        self.assertEqual(excluded.status_code, 404)
        self.assertEqual(_CountingSession.calls, [])

    def test_owner_state_api_needs_the_rows_management(self):
        with without_commit_drain():
            interface = Interface.objects.create(device=self.device_b, name="Ethernet31", type="1000base-t")
            row = acquire_overlay(NSOInterfaceState, interface=interface, attribute="description")
        _grant(self.user, ("view",), NSOInterfaceState)
        _grant(self.user, ("view",), Device)
        _grant(self.user, ("view",), NSODeviceManagement, {"pk": self.management_a.pk})
        url = reverse("plugins-api:netbox_nso_plugin-api:owner-state-detail", args=(row._meta.model_name, row.pk))
        self.assertEqual(self._api().get(url).status_code, 404)

    def test_onboarding_needs_a_visible_device(self):
        _grant(self.user, ("add",), NSODeviceManagement)
        _grant(self.user, ("view",), Device, {"pk": self.device_a.pk})
        unmanaged = Device.objects.create(
            name="authz-unmanaged",
            device_type=self.device_a.device_type,
            role=self.device_a.role,
            site=self.device_a.site,
        )
        response = self.client.post(reverse("plugins:netbox_nso_plugin:onboard"), {"device": unmanaged.pk})
        self.assertEqual(response.status_code, 404)
        self.assertFalse(NSODeviceManagement.objects.filter(device=unmanaged).exists())
        self.assertEqual(_CountingSession.calls, [])

    def test_shared_versions_leave_out_hidden_devices(self):
        from django.contrib.contenttypes.models import ContentType
        from netbox_routing.models import PrefixList

        from netbox_nso_plugin.models import NSORoutePolicyState

        policy = PrefixList.objects.create(name="authz-shared")
        rows = [
            acquire_overlay(
                NSORoutePolicyState,
                management=management,
                family="prefix_list",
                object_name=policy.name,
                content_type=ContentType.objects.get_for_model(policy),
                object_id=policy.pk,
                status="imported",
            )
            for management in (self.management_a, self.management_b)
        ]
        # Management view is unconstrained; only the device restriction hides B's version.
        self._view_device_a_only()
        response = self.client.get(
            reverse("plugins:netbox_nso_plugin:routing_route_policy_versions", kwargs={"pk": rows[0].pk})
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["row"].pk for item in response.context["items"]], [rows[0].pk])
        hidden = self.client.get(
            reverse("plugins:netbox_nso_plugin:routing_route_policy_versions", kwargs={"pk": rows[1].pk})
        )
        self.assertEqual(hidden.status_code, 404)

    def test_a_shared_vlan_rename_needs_every_attached_management(self):
        from ipam.models import VLAN

        from netbox_nso_plugin.models import NSOVLANState

        vlan = VLAN.objects.create(vid=919, name="AUTHZ-KEEP")
        row_a, _row_b = (
            acquire_overlay(NSOVLANState, management=management, vlan=vlan, device_name="AUTHZ-KEEP", status="imported")
            for management in (self.management_a, self.management_b)
        )
        _grant(self.user, ("change",), NSODeviceManagement, {"pk": self.management_a.pk})
        _grant(self.user, ("change",), VLAN)
        response = self.client.post(
            reverse("plugins:netbox_nso_plugin:overlay_field_edit", kwargs={"key": "vlan_name", "pk": row_a.pk}),
            {"name": "AUTHZ-RENAMED"},
        )
        self.assertEqual(response.status_code, 404)
        vlan.refresh_from_db()
        self.assertEqual(vlan.name, "AUTHZ-KEEP")

    def test_bulk_delete_of_all_rows_stays_inside_the_restriction(self):
        """The overridden bulk delete selects ``_all`` rows from the delete-restricted queryset."""
        rows = []
        with without_commit_drain():
            for device, name in ((self.device_a, "Ethernet21"), (self.device_b, "Ethernet22")):
                interface = Interface.objects.create(device=device, name=name, type="1000base-t")
                rows.append(acquire_overlay(NSOInterfaceState, interface=interface, attribute="description"))
        _grant(self.user, ("view", "delete"), NSOInterfaceState, {"pk": rows[0].pk})
        response = self.client.post(
            reverse("plugins:netbox_nso_plugin:nsointerfacestate_bulk_delete"),
            {"pk": [rows[0].pk], "_all": "1", "_confirm": "1", "confirm": "true"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(NSOInterfaceState.objects.filter(pk=rows[0].pk).exists())
        self.assertTrue(NSOInterfaceState.objects.filter(pk=rows[1].pk).exists())


class TestReplayReauthorizes(IntentPushResetMixin, TestCase):
    """A bulk delete replayed through ``process_request_as_job`` re-runs the policy at execution time."""

    def setUp(self):
        super().setUp()
        import django_rq
        from rq import Queue

        self.queue = Queue(f"nso-authz-{uuid4().hex[:8]}", connection=django_rq.get_queue("default").connection)
        self.addCleanup(self.queue.delete, delete_jobs=True)
        queued = patch("django_rq.get_queue", return_value=self.queue)
        queued.start()
        self.addCleanup(queued.stop)
        device, _management = make_managed("authz-replay", 17095)
        with without_commit_drain():
            self.row = acquire_overlay(
                NSOInterfaceState,
                interface=Interface.objects.create(device=device, name="Ethernet5", type="1000base-t"),
                attribute="description",
                status="imported",
            )
        self.user = User.objects.create_user(username=f"authz-replay-{uuid4().hex[:6]}")
        self.permission = _grant(self.user, ("view", "delete"), NSOInterfaceState)
        self.client.force_login(self.user)

    def _enqueue_bulk_delete(self):
        url = reverse("plugins:netbox_nso_plugin:nsointerfacestate_bulk_delete")
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                url, {"pk": [self.row.pk], "_confirm": "1", "confirm": "true", "background_job": "on"}
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(self.queue.job_ids), 1)

    def _drain(self):
        from rq import SimpleWorker

        SimpleWorker([self.queue], connection=self.queue.connection).work(burst=True, with_scheduler=False)

    def test_a_permission_revoked_before_replay_refuses_the_delete(self):
        from core.models import Job

        self._enqueue_bulk_delete()
        self.permission.delete()
        self._drain()
        self.assertTrue(NSOInterfaceState.objects.filter(pk=self.row.pk).exists())
        self.assertEqual(Job.objects.get(user=self.user).status, "errored")

    def test_the_replay_deletes_while_the_permission_holds(self):
        self._enqueue_bulk_delete()
        self._drain()
        self.assertFalse(NSOInterfaceState.objects.filter(pk=self.row.pk).exists())
