# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from django.urls import path
from netbox.api.routers import NetBoxRouter

from . import views

app_name = "netbox_nso_plugin"

router = NetBoxRouter()
router.register("derived-intent-templates", views.NSODerivedIntentTemplateViewSet)
router.register("instances", views.NSOInstanceViewSet)
router.register("ned-mappings", views.NSOPlatformNedMappingViewSet)
router.register("device-management", views.NSODeviceManagementViewSet)
router.register("interface-state", views.NSOInterfaceStateViewSet)
router.register("link-roles", views.NSOLinkRoleViewSet)
router.register("link-role-assignments", views.NSOLinkRoleAssignmentViewSet)

urlpatterns = [
    path("sync-complete/", views.SyncCompleteView.as_view(), name="sync_complete"),
    path("provision-complete/", views.ProvisionCompleteView.as_view(), name="provision_complete"),
    path("onboarding-candidates/", views.OnboardingCandidatesView.as_view(), name="onboarding_candidates"),
    path("onboard/", views.OnboardView.as_view(), name="onboard"),
    *router.urls,
]
