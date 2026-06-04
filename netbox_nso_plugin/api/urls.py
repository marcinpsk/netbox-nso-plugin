# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from django.urls import path
from netbox.api.routers import NetBoxRouter

from . import views

app_name = "netbox_nso_plugin"

router = NetBoxRouter()
router.register("instances", views.NSOInstanceViewSet)
router.register("ned-mappings", views.NSOPlatformNedMappingViewSet)
router.register("device-management", views.NSODeviceManagementViewSet)
router.register("interface-state", views.NSOInterfaceStateViewSet)

urlpatterns = [
    path("sync-complete/", views.SyncCompleteView.as_view(), name="sync_complete"),
    path("onboarding-candidates/", views.OnboardingCandidatesView.as_view(), name="onboarding_candidates"),
    path("onboard/", views.OnboardView.as_view(), name="onboard"),
    *router.urls,
]
