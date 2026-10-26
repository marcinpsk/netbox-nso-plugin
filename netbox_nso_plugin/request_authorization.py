# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Endpoint authorization: every plugin view declares one policy, and its class enforces it.

A Django view runs the policy in a final ``dispatch``; a DRF view runs it in a final
``initial``, after DRF authentication, and keeps the authority scope open through the
handler. The policy checks model-level permissions (403), then resolves the target
through restricted querysets (404), before any handler code runs.
"""

from __future__ import annotations

import contextlib
import operator
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import BadRequest, ImproperlyConfigured, PermissionDenied
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import URLResolver, reverse
from django.views import View

from .native_authorization import SharedOperationConflict, authority_scope, keep_authorized_target

Effect = Literal["read", "native_mutation", "adapter_action"]
EFFECTS: tuple[str, ...] = ("read", "native_mutation", "adapter_action")
_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "trace"})
_HANDLER_METHODS = ("get", "post", "put", "patch", "delete")


# ── Authorization target registry (separate from the OVERLAYS settlement registry) ──


@dataclass(frozen=True)
class AuthzPath:
    """ORM paths from an overlay row to its management row and its device."""

    management: str
    device: str

    def management_of(self, row):
        """Return the management row that owns *row*."""
        return operator.attrgetter(self.management.replace("__", "."))(row)


_BY_MANAGEMENT = AuthzPath("management", "management__device")
_BY_INTERFACE = AuthzPath("interface__device__nso_management", "interface__device")

AUTHZ_TARGETS: Mapping[str, AuthzPath] = MappingProxyType(
    {
        "netbox_nso_plugin.nsointerfacestate": _BY_INTERFACE,
        "netbox_nso_plugin.nsointerfaceipstate": _BY_INTERFACE,
        **{
            f"netbox_nso_plugin.{name}": _BY_MANAGEMENT
            for name in (
                "nsosnmpcommunitystate",
                "nsosnmpv3userstate",
                "nsosnmphoststate",
                "nsosnmpsysteminfostate",
                "nsologginghoststate",
                "nsologginglevelstate",
                "nsosvistate",
                "nsosubinterfacestate",
                "nsointerfacemtustate",
                "nsovlanstate",
                "nsoswitchportstate",
                "nsobfdinterfacestate",
                "nsostaticroutestate",
                "nsoisisflexalgostate",
                "nsol2sapstate",
                "nsoisisinstancestate",
                "nsoisisinterfacestate",
                "nsobgprouterstate",
                "nsobgppeerstate",
                "nsobgppeertemplatestate",
                "nsoroutepolicystate",
                "nsoospfinstancestate",
                "nsoospfinterfacestate",
                "nsoredistributionstate",
                "nsolacpbundlestate",
                "nsolacpmemberstate",
            )
        },
    }
)


def missing_authz_targets(labels) -> list[str]:
    """Return the model labels that have no authorization target path."""
    return sorted(label for label in labels if label not in AUTHZ_TARGETS)


# ── The authorized target handed to the handler ──


@dataclass(frozen=True)
class AuthorizedTarget:
    """The objects an endpoint may act on, resolved through restricted querysets."""

    user: Any
    primary: Any = None
    device: Any = None
    management: Any = None
    queryset: Any = None
    related: Mapping[str, Any] = field(default_factory=dict)

    def reload(self):
        """Re-read the primary row through the restricted queryset that authorized it."""
        return get_object_or_404(self.queryset, pk=self.primary.pk)

    def scoped(self, model):
        """Return the rows of *model* that belong to the authorized management."""
        from .models import NSODeviceManagement

        if self.management is None:
            return model.objects.none()
        if model is NSODeviceManagement:
            return model.objects.filter(pk=self.management.pk)
        return model.objects.filter(**{AUTHZ_TARGETS[model._meta.label_lower].management: self.management})

    def allows(self, action):
        """Return whether the user may perform *action* on the authorized management."""
        return (
            self.management is not None
            and self.user.has_perm(f"netbox_nso_plugin.{action}_nsodevicemanagement")
            and _managements(self.user, action).filter(pk=self.management.pk).exists()
        )

    def require(self, action):
        """Require *action* on the authorized management: 403 without the permission, 404 outside it."""
        if not self.user.has_perm(f"netbox_nso_plugin.{action}_nsodevicemanagement"):
            raise PermissionDenied
        if not self.allows(action):
            raise Http404

    @staticmethod
    def selected(queryset, pks):
        """Return the selected rows, or 404 when any requested key is outside *queryset*."""
        wanted = set(pks)
        rows = list(queryset.filter(pk__in=wanted))
        if len(rows) != len(wanted):
            raise Http404
        return rows


# ── Target kinds ──


def _device_model():
    from dcim.models import Device

    return Device


def _managements(user, action):
    from .models import NSODeviceManagement

    return NSODeviceManagement.objects.restrict(user, action).select_related("device")


def _device_management(user, device, action):
    """Return the device's management through *action*, None when the device is unmanaged.

    A management row that exists but lies outside the restriction is 404: an action
    must not treat a hidden management as absent.
    """
    from .models import NSODeviceManagement

    management_pk = NSODeviceManagement.objects.filter(device=device).values_list("pk", flat=True).first()
    if management_pk is None:
        return None
    if not user.has_perm(f"netbox_nso_plugin.{action}_nsodevicemanagement"):
        raise PermissionDenied
    return get_object_or_404(_managements(user, action), pk=management_pk)


def overlay_row(model, pk, user, action, *, base=None) -> AuthorizedTarget:
    """Resolve one overlay row inside the managements (and, for reads, devices) the user may use.

    An interface overlay on an unmanaged device has no management authority: its device authorizes
    it, through view for a read and through change for any other action (NetBox semantics).
    """
    path = AUTHZ_TARGETS[model._meta.label_lower]
    rows = model.objects.all() if base is None else base
    queryset = rows.filter(**{f"{path.management}__in": _managements(user, action)})
    if action == "view":
        queryset = queryset.filter(**{f"{path.device}__in": _device_model().objects.restrict(user, "view")})
    row = queryset.filter(pk=pk).first()
    if row is not None:
        management = path.management_of(row)
        return AuthorizedTarget(
            user=user, primary=row, device=management.device, management=management, queryset=queryset
        )
    if path is not _BY_INTERFACE or not rows.filter(pk=pk, interface__device__nso_management__isnull=True).exists():
        raise Http404
    device_action = "view" if action == "view" else "change"
    if not user.has_perm(f"dcim.{device_action}_device"):
        raise PermissionDenied
    queryset = rows.filter(
        interface__device__nso_management__isnull=True,
        interface__device__in=_device_model().objects.restrict(user, device_action),
    )
    row = get_object_or_404(queryset, pk=pk)
    return AuthorizedTarget(user=user, primary=row, device=row.interface.device, queryset=queryset)


def authorize_related_row(row, user, action):
    """Authorize a second overlay row an operation touches: 403 without the permission, 404 outside it."""
    keep_authorized_target(overlay_row(type(row), row.pk, user, action))


def _management_permissions(action, *, device_view=False):
    permissions = (f"netbox_nso_plugin.{action}_nsodevicemanagement",)
    return (*permissions, "dcim.view_device") if device_view else permissions


class TargetSpec:
    """Declare how an endpoint finds its target. Subclasses resolve only through restricted querysets."""

    def permissions(self) -> tuple[str, ...]:
        """Return the model-level permissions the endpoint needs (missing: 403)."""
        return ()

    def resolve(self, request, user, kwargs) -> AuthorizedTarget:
        """Return the authorized target, or raise Http404 when it is outside the user's restriction."""
        raise NotImplementedError


class NetBoxGeneric(TargetSpec):
    """NetBox's generic view or viewset restricts its own queryset by the requested action."""

    def resolve(self, request, user, kwargs):  # noqa: D102
        return AuthorizedTarget(user=user)


@dataclass(frozen=True)
class Collection(TargetSpec):
    """A page with no single target. The handler lists only through restricted querysets."""

    required: tuple[str, ...] = ()

    def permissions(self):  # noqa: D102
        return self.required

    def resolve(self, request, user, kwargs):  # noqa: D102
        return AuthorizedTarget(user=user)


@dataclass(frozen=True)
class DeviceRead(TargetSpec):
    """A read of one device page: the device and, when it is managed, its management must be visible."""

    kwarg: str = "pk"

    def permissions(self):  # noqa: D102
        return ("dcim.view_device",)

    def resolve(self, request, user, kwargs):  # noqa: D102
        devices = _device_model().objects.restrict(user, "view")
        device = get_object_or_404(devices, pk=kwargs[self.kwarg])
        management = _device_management(user, device, "view")
        return AuthorizedTarget(user=user, primary=device, device=device, management=management, queryset=devices)


@dataclass(frozen=True)
class Management(TargetSpec):
    """A management action, addressed by the management key or by its device key."""

    action: str
    kwarg: str = "pk"
    by: Literal["pk", "device"] = "pk"
    device_view: bool = False
    optional: bool = False

    def permissions(self):  # noqa: D102
        return _management_permissions(self.action, device_view=self.device_view)

    def resolve(self, request, user, kwargs):  # noqa: D102
        value = kwargs[self.kwarg]
        managements = _managements(user, self.action)
        if self.by == "device":
            devices = _device_model().objects.restrict(user, "view")
            if self.optional:
                from .models import NSODeviceManagement

                if not NSODeviceManagement.objects.filter(device_id=value).exists():
                    device = get_object_or_404(devices, pk=value)
                    return AuthorizedTarget(user=user, primary=device, device=device, queryset=devices)
            management = get_object_or_404(managements, device_id=value)
        else:
            management = get_object_or_404(managements, pk=value)
        if self.device_view:
            get_object_or_404(_device_model().objects.restrict(user, "view"), pk=management.device_id)
        return AuthorizedTarget(
            user=user, primary=management, device=management.device, management=management, queryset=managements
        )


@dataclass(frozen=True)
class OverlayRow(TargetSpec):
    """One overlay row, authorized through its management (AUTHZ_TARGETS path)."""

    model: type
    action: str
    kwarg: str = "pk"

    def permissions(self):  # noqa: D102
        return _management_permissions(self.action, device_view=self.action == "view")

    def resolve(self, request, user, kwargs):  # noqa: D102
        return overlay_row(self.model, kwargs[self.kwarg], user, self.action)


@dataclass(frozen=True)
class OverlayFamily(TargetSpec):
    """One overlay row whose model the URL family key selects; a shared family adds its rule."""

    families: Mapping[str, type]
    action: str
    rules: Mapping[str, Callable] = field(default_factory=dict)

    def permissions(self):  # noqa: D102
        return _management_permissions(self.action)

    def resolve(self, request, user, kwargs):  # noqa: D102
        model = self.families.get(kwargs["key"])
        if model is None:
            raise Http404
        target = overlay_row(model, kwargs["pk"], user, self.action)
        rule = self.rules.get(kwargs["key"])
        if rule is not None:
            rule(user, target.primary)
        return target


def shared_vlan_rename(user, state):
    """Require change on the shared native root before validating a VLAN edit."""
    from ipam.models import VLAN

    if not user.has_perm("ipam.change_vlan"):
        raise PermissionDenied
    get_object_or_404(VLAN.objects.restrict(user, "change"), pk=state.vlan_id)


def is_valid_instance_id(value):
    """Validate an instance selection before the ORM receives it."""
    from rest_framework import serializers

    if value is None:
        return True
    if not isinstance(value, str):
        return False
    try:
        serializers.CharField(allow_blank=True, trim_whitespace=False).run_validation(value)
    except serializers.ValidationError:
        return False
    return True


def permitted_instance(user, selected):
    """Return the selected NSO instance, else the default one, through the view restriction (pages only).

    An excluded selection falls back like an unknown one. A default the user may not view
    is not offered. A mutation resolves an explicit selection strictly instead.
    """
    from .models import NSOInstance

    if not is_valid_instance_id(selected):
        raise BadRequest("Invalid NSO instance identity")
    visible = NSOInstance.objects.restrict(user, "view")
    instance = visible.filter(adapter_instance_id=selected).first() if selected else None
    if instance is not None:
        return instance
    default = NSOInstance.get_default()
    return default if default is not None and visible.filter(pk=default.pk).exists() else None


def parse_object_id(value: str) -> int | None:
    """Parse an ASCII decimal object ID. Return None if conversion fails."""
    if not (value.isascii() and value.isdecimal()):
        return None
    try:
        return int(value)
    except ValueError:
        # Python rejects decimal strings that exceed its integer digit limit.
        return None


@dataclass(frozen=True)
class Onboarding(TargetSpec):
    """Onboard a device: the posted device and the selected instance must be visible (404 otherwise)."""

    def permissions(self):  # noqa: D102
        return ("netbox_nso_plugin.add_nsodevicemanagement", "dcim.view_device")

    def resolve(self, request, user, kwargs):  # noqa: D102
        from .models import NSOInstance

        device_id = parse_object_id(request.POST.get("device", ""))
        if device_id is None:
            raise Http404
        device = get_object_or_404(_device_model().objects.restrict(user, "view"), pk=device_id)
        selected = request.POST.get("instance")
        if not is_valid_instance_id(selected):
            raise BadRequest("Invalid NSO instance identity")
        if selected:
            # A mutation never substitutes the default for an explicit selection.
            visible = NSOInstance.objects.restrict(user, "view")
            instance = get_object_or_404(visible, adapter_instance_id=selected)
        else:
            instance = permitted_instance(user, None)
        return AuthorizedTarget(user=user, primary=device, device=device, related={"instance": instance})


@dataclass(frozen=True)
class Instance(TargetSpec):
    """One NSO instance through the view restriction."""

    kwarg: str = "pk"

    def permissions(self):  # noqa: D102
        return ("netbox_nso_plugin.view_nsoinstance",)

    def resolve(self, request, user, kwargs):  # noqa: D102
        from .models import NSOInstance

        instances = NSOInstance.objects.restrict(user, "view")
        instance = get_object_or_404(instances, pk=kwargs[self.kwarg])
        return AuthorizedTarget(user=user, primary=instance, queryset=instances)


# ── API (DRF) target kinds: request bodies are parsed here, after DRF authentication ──


def _request_body(request):
    from rest_framework.exceptions import ParseError

    body = request.data
    if not isinstance(body, dict):
        raise ParseError("request body must be a JSON object")
    return body


@dataclass(frozen=True)
class OwnerRow(TargetSpec):
    """A registered overlay row by model label and key: overlay view plus management view."""

    def resolve(self, request, user, kwargs):  # noqa: D102
        from rest_framework.exceptions import NotFound

        from .refusal_registry import OVERLAYS

        spec = OVERLAYS.get(f"netbox_nso_plugin.{kwargs['model']}")
        if spec is None:
            raise NotFound("Unknown owner model.")
        required = (
            f"netbox_nso_plugin.view_{spec.model._meta.model_name}",
            "netbox_nso_plugin.view_nsodevicemanagement",
            "dcim.view_device",
        )
        if not all(user.has_perm(permission) for permission in required):
            raise PermissionDenied
        base = spec.model.objects.restrict(user, "view")
        if spec.model_label == "netbox_nso_plugin.nsobgprouterstate":
            from .views import _visible_bgp_router_states

            base = base.filter(pk__in=_visible_bgp_router_states(user).values("pk"))
        return overlay_row(spec.model, kwargs["pk"], user, "view", base=base)


@dataclass(frozen=True)
class IntegrationSync(TargetSpec):
    """The adapter's sync-complete callback: the integration action on the reported management."""

    action: str = "sync"

    def permissions(self):  # noqa: D102
        return (f"netbox_nso_plugin.{self.action}_nsodevicemanagement",)

    def resolve(self, request, user, kwargs):  # noqa: D102
        from rest_framework.exceptions import NotFound, ParseError

        body = _request_body(request)
        device_id = body.get("netbox_device_id")
        adapter_device_id = body.get("adapter_device_id")
        if any(
            value is not None and (type(value) is not int or value <= 0) for value in (device_id, adapter_device_id)
        ):
            raise ParseError("device identities must be positive integers")
        if device_id is None and adapter_device_id is None:
            raise ParseError("netbox_device_id or adapter_device_id is required")
        managements = _managements(user, self.action)
        if device_id is not None:
            management = managements.filter(device_id=device_id).first()
            if management is None:
                raise NotFound(f"no managed device for netbox_device_id={device_id}")
            if adapter_device_id is not None and management.adapter_device_id != adapter_device_id:
                raise ParseError("device identities do not match")
        else:
            management = managements.filter(adapter_device_id=adapter_device_id).first()
            if management is None:
                raise NotFound(f"no managed device for adapter_device_id={adapter_device_id}")
        return AuthorizedTarget(
            user=user, primary=management, device=management.device, management=management, queryset=managements
        )


@dataclass(frozen=True)
class IntegrationProvisionAttempt(TargetSpec):
    """The adapter's provision-complete callback, bound to the durable attempt identity.

    The tombstone keeps a scalar device id, so evidence for an attempt whose device or
    management is gone is still accepted; no device-scoped row is written.
    """

    def permissions(self):  # noqa: D102
        return ("netbox_nso_plugin.change_nsoprovisiontombstone",)

    def resolve(self, request, user, kwargs):  # noqa: D102
        from uuid import UUID

        from rest_framework.exceptions import ParseError
        from utilities.querysets import RestrictedQuerySet

        from .models import NSOProvisionTombstone

        try:
            body = _request_body(request)
        except ParseError:
            raise ParseError("invalid provision completion evidence") from None
        raw = body.get("provision_attempt_id")
        if raw in (None, ""):
            raise ParseError("provision_attempt_id is required")
        try:
            attempt_id = UUID(str(raw))
        except (TypeError, ValueError):
            raise ParseError("invalid provision completion evidence") from None
        # The tombstone model has a plain manager; NetBox's restricted queryset applies the constraints.
        attempts = RestrictedQuerySet(model=NSOProvisionTombstone).restrict(user, "change")
        tombstone = attempts.filter(provision_attempt_id=attempt_id).first()
        if tombstone is None and NSOProvisionTombstone.objects.filter(provision_attempt_id=attempt_id).exists():
            raise Http404
        return AuthorizedTarget(
            user=user, primary=tombstone, queryset=attempts, related={"attempt_id": attempt_id, "body": body}
        )


@dataclass(frozen=True)
class ApiOnboarding(TargetSpec):
    """The API onboard action: the body's device must be visible, the instance through the view restriction.

    Onboarding creates the management row, so the API needs add, as the UI onboard does.
    """

    def permissions(self):  # noqa: D102
        return ("netbox_nso_plugin.add_nsodevicemanagement", "dcim.view_device")

    def resolve(self, request, user, kwargs):  # noqa: D102
        from rest_framework.exceptions import NotFound, ParseError

        from .models import NSOInstance

        body = _request_body(request)
        device_id = body.get("netbox_device_id")
        if device_id is None:
            raise ParseError("netbox_device_id is required")
        if type(device_id) is not int or device_id <= 0:
            raise ParseError("netbox_device_id must be a positive integer")
        device = _device_model().objects.restrict(user, "view").filter(pk=device_id).first()
        if device is None:
            raise NotFound(f"device {device_id} not found")
        selected = body.get("instance")
        if not is_valid_instance_id(selected):
            raise ParseError("Invalid NSO instance identity")
        if selected:
            instance = NSOInstance.objects.restrict(user, "view").filter(adapter_instance_id=selected).first()
            if instance is None:
                raise NotFound(f"unknown instance '{selected}'")
        else:
            instance = permitted_instance(user, None)
        if instance is None:
            raise NotFound("no NSO instance configured")
        return AuthorizedTarget(user=user, primary=device, device=device, related={"instance": instance})


# ── The policy ──


@dataclass(frozen=True)
class EndpointPolicy:
    """What an endpoint acts on, and which native-write authority its handler has."""

    target: TargetSpec
    effects: Effect
    permissions: tuple[str, ...] = ()

    def __post_init__(self):
        if not isinstance(self.target, TargetSpec):
            raise ImproperlyConfigured(f"policy target must be a TargetSpec, not {self.target!r}")
        if self.effects not in EFFECTS:
            raise ImproperlyConfigured(f"unknown endpoint effect {self.effects!r}")
        if isinstance(self.permissions, str) or not all(isinstance(p, str) for p in self.permissions):
            raise ImproperlyConfigured("policy permissions must be a tuple of permission names")

    def authorize(self, request, user, kwargs) -> AuthorizedTarget:
        """Check model-level permissions (403), then resolve the target (404)."""
        required = (*self.target.permissions(), *self.permissions)
        if not all(user.has_perm(permission) for permission in required):
            raise PermissionDenied
        return self.target.resolve(request, user, kwargs)


def generic_policy(*, read=("get", "head", "options"), mutate=("post", "put", "patch", "delete")):
    """Return the per-method policy of a NetBox generic view: reads in the no-write scope."""
    return MappingProxyType(
        {
            **{method: EndpointPolicy(NetBoxGeneric(), "read") for method in read},
            **{method: EndpointPolicy(NetBoxGeneric(), "native_mutation") for method in mutate},
        }
    )


def policy_for(view, method: str) -> EndpointPolicy | None:
    """Return the policy that governs *method* on *view*, or None when none is declared."""
    policy = type(view).policy
    if isinstance(policy, EndpointPolicy):
        return policy
    method = method.lower()
    if method in policy:
        return policy[method]
    if method == "head":
        return policy.get("get")
    return None


# ── Enforcement in the class ──


def _acting_user(request):
    """Return the requesting user; a replayed background request reloads it from the database."""
    user = request.user
    if hasattr(request, "job") and user.is_authenticated:
        try:
            user = type(user).objects.get(pk=user.pk)
        except type(user).DoesNotExist:
            raise PermissionDenied from None
        request.user = user
    return user


def _is_netbox_generic(cls) -> bool:
    from netbox.api.viewsets import BaseViewSet
    from netbox.views.generic.feature_views import ObjectChangeLogView, ObjectJournalView
    from utilities.views import ObjectPermissionRequiredMixin

    return issubclass(cls, (ObjectPermissionRequiredMixin, ObjectChangeLogView, ObjectJournalView, BaseViewSet))


def _check_entry_points(cls):
    for klass in cls.__mro__:
        if klass.__dict__.get("_authz_enforcer"):
            return
        for name in cls._authz_final:
            if name in klass.__dict__:
                raise ImproperlyConfigured(
                    f"{cls.__qualname__}: {klass.__qualname__} overrides {name}(), where the endpoint policy runs"
                )
    raise ImproperlyConfigured(f"{cls.__qualname__} does not reach the endpoint policy enforcement")


def _check_policy(cls):
    policy = cls.__dict__.get("policy")
    if policy is None:
        raise ImproperlyConfigured(f"{cls.__qualname__} declares no endpoint policy")
    if isinstance(policy, EndpointPolicy):
        policies = [policy]
    elif isinstance(policy, Mapping):
        unknown = set(policy) - _HTTP_METHODS
        if unknown or not all(isinstance(value, EndpointPolicy) for value in policy.values()):
            raise ImproperlyConfigured(f"{cls.__qualname__} has a malformed per-method policy")
        if not cls._authz_api:
            uncovered = [m for m in _HANDLER_METHODS if callable(getattr(cls, m, None)) and m not in policy]
            if uncovered:
                raise ImproperlyConfigured(f"{cls.__qualname__} has no policy for {', '.join(uncovered)}")
        policies = list(policy.values())
    else:
        raise ImproperlyConfigured(f"{cls.__qualname__}.policy must be an EndpointPolicy or a per-method mapping")
    if any(isinstance(p.target, NetBoxGeneric) for p in policies) and not _is_netbox_generic(cls):
        raise ImproperlyConfigured(f"{cls.__qualname__} uses NetBoxGeneric but is not a NetBox generic view")


class _PolicyEnforced:
    """Reject at class creation a concrete view without a policy, or one that overrides the entry point."""

    _authz_enforcer = True
    _authz_final: tuple[str, ...] = ()
    _authz_abstract = True
    _authz_api = False
    policy: EndpointPolicy | Mapping[str, EndpointPolicy]
    target: AuthorizedTarget

    def __init_subclass__(cls, *, abstract: bool = False, **kwargs):
        super().__init_subclass__(**kwargs)
        cls._authz_abstract = abstract
        if "_authz_enforcer" in cls.__dict__:
            return
        _check_entry_points(cls)
        if not abstract:
            _check_policy(cls)


def _device_nso_tab_url(device_pk):
    return reverse("dcim:device_nso", kwargs={"pk": device_pk})


class AuthorizedView(_PolicyEnforced, LoginRequiredMixin, View):
    """Base of every custom plugin Django view. ``dispatch`` is final."""

    _authz_enforcer = True
    _authz_final = ("dispatch",)

    def dispatch(self, request, *args, **kwargs):
        """Run the endpoint policy, then the handler inside its authority scope."""
        from .ownership_planner import OwnershipNotQualified

        if not request.user.is_authenticated:
            return self.handle_no_permission()
        policy = policy_for(self, request.method)
        if policy is None:
            return self.http_method_not_allowed(request, *args, **kwargs)
        from utilities.exceptions import PermissionsViolation

        user = _acting_user(request)
        self.target = policy.authorize(request, user, kwargs)
        is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest" or "key" in kwargs
        try:
            with authority_scope(policy.effects, user, target=self.target):
                try:
                    return super().dispatch(request, *args, **kwargs)
                except OwnershipNotQualified as exc:
                    if is_ajax or exc.device_id is None:
                        return JsonResponse({"status": "error", "message": exc.public_message}, status=400)
                    messages.error(request, exc.public_message)
                    return redirect(_device_nso_tab_url(exc.device_id))
        except SharedOperationConflict as exc:
            if is_ajax or self.target.device is None:
                return JsonResponse({"status": "error", "message": exc.public_message}, status=409)
            messages.error(request, exc.public_message)
            return redirect(_device_nso_tab_url(self.target.device.pk))
        except PermissionsViolation as exc:
            # The operator scope rolled back every write of the request.
            if is_ajax:
                return JsonResponse({"status": "error", "message": exc.message}, status=403)
            raise PermissionDenied(exc.message) from None


class AuthorizedGenericView(_PolicyEnforced):
    """Mixin placed before a NetBox generic view. ``dispatch`` is final; NetBox restricts the queryset."""

    _authz_enforcer = True
    _authz_final = ("dispatch",)

    def dispatch(self, request, *args, **kwargs):
        """Run the endpoint policy, then NetBox's own dispatch inside the authority scope."""
        policy = policy_for(self, request.method)
        if policy is None:
            return self.http_method_not_allowed(request, *args, **kwargs)
        self.target = policy.authorize(request, _acting_user(request), kwargs)
        # NetBox's generic views authorize their own object writes (design 2b): no operator scope.
        native = policy.effects == "native_mutation"
        with contextlib.nullcontext() if native else authority_scope(policy.effects):
            return super().dispatch(request, *args, **kwargs)


class AuthorizedAPIView(_PolicyEnforced):
    """Mixin placed before a DRF view. ``dispatch`` and ``initial`` are final."""

    _authz_enforcer = True
    _authz_final = ("dispatch", "initial")
    _authz_api = True

    def dispatch(self, request, *args, **kwargs):
        """Keep the authority scope that ``initial`` opens until the response is final."""
        from rest_framework.response import Response
        from utilities.exceptions import PermissionsViolation

        try:
            with contextlib.ExitStack() as stack:
                self._authz_scope = stack
                return super().dispatch(request, *args, **kwargs)
        except SharedOperationConflict as exc:
            return self.finalize_response(
                self.request, Response({"detail": exc.public_message}, status=409), *args, **kwargs
            )
        except PermissionsViolation as exc:
            # The operator scope rolled back every write of the request.
            return self.finalize_response(self.request, Response({"detail": exc.message}, status=403), *args, **kwargs)

    def initial(self, request, *args, **kwargs):
        """Authenticate (DRF), then run the endpoint policy and open its authority scope."""
        from rest_framework.exceptions import MethodNotAllowed

        super().initial(request, *args, **kwargs)
        policy = policy_for(self, request.method)
        if policy is None:
            raise MethodNotAllowed(request.method)
        user = _acting_user(request)
        self.target = policy.authorize(request, user, kwargs)
        if isinstance(policy.target, NetBoxGeneric) and policy.effects == "native_mutation":
            # A NetBox viewset authorizes its own object writes (design 2b): no operator scope.
            return
        self._authz_scope.enter_context(authority_scope(policy.effects, user, target=self.target))


def view_class_of(callback):
    """Return the class behind a URL callback, or None for a function view."""
    return getattr(callback, "view_class", None) or getattr(callback, "cls", None)


def uncovered_routes(patterns, prefix: str = "") -> list[str]:
    """Return every route whose view does not enforce an endpoint policy."""
    found = []
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            found.extend(uncovered_routes(pattern.url_patterns, prefix + str(pattern.pattern)))
            continue
        cls = view_class_of(pattern.callback)
        if cls is None or not issubclass(cls, _PolicyEnforced) or cls._authz_abstract:
            found.append(f"{prefix}{pattern.pattern}")
    return found


def collection_views(classes) -> set[str]:
    """Return the views that declare a Collection target (no single target; census-pinned)."""
    found = set()
    for cls in classes:
        policy = cls.policy
        policies = [policy] if isinstance(policy, EndpointPolicy) else list(policy.values())
        if any(isinstance(p.target, Collection) for p in policies):
            found.add(cls.__qualname__)
    return found
