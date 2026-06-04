# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from django.urls import path
from netbox.views.generic.feature_views import ObjectChangeLogView, ObjectJournalView

from . import views
from .models import NSODeviceManagement, NSOInstance, NSOInterfaceState, NSOPlatformNedMapping

app_name = "netbox_nso_plugin"

urlpatterns = [
    # Adapter Connection (singleton)
    path("adapter-connection/", views.AdapterConnectionEditView.as_view(), name="adapterconnection"),
    # NSO Instances
    path("instances/", views.NSOInstanceListView.as_view(), name="nsoinstance_list"),
    path("instances/add/", views.NSOInstanceEditView.as_view(), name="nsoinstance_add"),
    path("instances/<int:pk>/", views.NSOInstanceView.as_view(), name="nsoinstance"),
    path("instances/<int:pk>/edit/", views.NSOInstanceEditView.as_view(), name="nsoinstance_edit"),
    path("instances/<int:pk>/delete/", views.NSOInstanceDeleteView.as_view(), name="nsoinstance_delete"),
    path(
        "instances/<int:pk>/changelog/",
        ObjectChangeLogView.as_view(),
        name="nsoinstance_changelog",
        kwargs={"model": NSOInstance},
    ),
    path(
        "instances/<int:pk>/journal/",
        ObjectJournalView.as_view(),
        name="nsoinstance_journal",
        kwargs={"model": NSOInstance},
    ),
    # Onboarding dashboard (tabs) + onboard action
    path("onboarding/", views.NSOOnboardingDashboardView.as_view(), name="onboarding_dashboard"),
    path("onboard/", views.NSOOnboardView.as_view(), name="onboard"),
    # Platform → NED mappings (onboarding)
    path("ned-mappings/", views.NSOPlatformNedMappingListView.as_view(), name="nsoplatformnedmapping_list"),
    path("ned-mappings/add/", views.NSOPlatformNedMappingEditView.as_view(), name="nsoplatformnedmapping_add"),
    path("ned-mappings/<int:pk>/", views.NSOPlatformNedMappingView.as_view(), name="nsoplatformnedmapping"),
    path(
        "ned-mappings/<int:pk>/edit/",
        views.NSOPlatformNedMappingEditView.as_view(),
        name="nsoplatformnedmapping_edit",
    ),
    path(
        "ned-mappings/<int:pk>/delete/",
        views.NSOPlatformNedMappingDeleteView.as_view(),
        name="nsoplatformnedmapping_delete",
    ),
    path(
        "ned-mappings/<int:pk>/changelog/",
        ObjectChangeLogView.as_view(),
        name="nsoplatformnedmapping_changelog",
        kwargs={"model": NSOPlatformNedMapping},
    ),
    # Device NSO tab — lazy per-category row load (HTML fragment)
    path(
        "devices/<int:pk>/category/<str:key>/",
        views.NSOCategoryView.as_view(),
        name="device_nso_category",
    ),
    # Device NSO tab — background "Refresh from NSO" (reconcile cache only)
    path(
        "device-management/<int:pk>/reconcile/",
        views.NSODeviceReconcileView.as_view(),
        name="nsodevicemanagement_reconcile",
    ),
    # Device Management CRUD
    path("device-management/", views.NSODeviceManagementListView.as_view(), name="nsodevicemanagement_list"),
    path("device-management/add/", views.NSODeviceManagementEditView.as_view(), name="nsodevicemanagement_add"),
    path("device-management/<int:pk>/", views.NSODeviceManagementView.as_view(), name="nsodevicemanagement"),
    path(
        "device-management/<int:pk>/edit/",
        views.NSODeviceManagementEditView.as_view(),
        name="nsodevicemanagement_edit",
    ),
    path(
        "device-management/<int:pk>/delete/",
        views.NSODeviceManagementDeleteView.as_view(),
        name="nsodevicemanagement_delete",
    ),
    path(
        "device-management/<int:pk>/changelog/",
        ObjectChangeLogView.as_view(),
        name="nsodevicemanagement_changelog",
        kwargs={"model": NSODeviceManagement},
    ),
    path(
        "device-management/<int:pk>/journal/",
        ObjectJournalView.as_view(),
        name="nsodevicemanagement_journal",
        kwargs={"model": NSODeviceManagement},
    ),
    # Adapter actions (sync / detect-drift / connect / apply)
    path(
        "device-management/<int:pk>/actions/<str:action>/",
        views.NSODeviceActionView.as_view(),
        name="nsodevicemanagement_action",
    ),
    # Compliance refresh (fetches live data and updates snapshot)
    path(
        "device-management/<int:pk>/refresh/",
        views.NSORefreshStateView.as_view(),
        name="nsodevicemanagement_refresh",
    ),
    # Adapter job status (JSON, for client-side polling)
    path("jobs/<int:job_id>/status/", views.NSOJobStatusView.as_view(), name="nsojob_status"),
    # Device job-activity summary (JSON: running + last finished) for the tab status strip
    path("devices/<int:pk>/jobs/", views.NSODeviceJobsView.as_view(), name="device_nso_jobs"),
    # Interface State CRUD (read-only list + detail; delete allowed for cleanup)
    path("interface-state/", views.NSOInterfaceStateListView.as_view(), name="nsointerfacestate_list"),
    path("interface-state/<int:pk>/", views.NSOInterfaceStateView.as_view(), name="nsointerfacestate"),
    path(
        "interface-state/<int:pk>/delete/",
        views.NSOInterfaceStateDeleteView.as_view(),
        name="nsointerfacestate_delete",
    ),
    path(
        "interface-state/<int:pk>/changelog/",
        ObjectChangeLogView.as_view(),
        name="nsointerfacestate_changelog",
        kwargs={"model": NSOInterfaceState},
    ),
    path(
        "interface-state/<int:pk>/journal/",
        ObjectJournalView.as_view(),
        name="nsointerfacestate_journal",
        kwargs={"model": NSOInterfaceState},
    ),
    # Accept workflow
    path(
        "interface-state/<int:pk>/accept/",
        views.NSOAcceptAttributeView.as_view(),
        name="nsointerfacestate_accept",
    ),
    path(
        "interface-state/<int:pk>/accept-device/",
        views.NSOAcceptDeviceView.as_view(),
        name="nsointerfacestate_accept_device",
    ),
    path(
        "interface-state/<int:pk>/edit-field/",
        views.NSOInterfaceEditFieldView.as_view(),
        name="nsointerfacestate_edit_field",
    ),
    path(
        "devices/<int:device_pk>/bulk-accept/",
        views.NSOBulkAcceptView.as_view(),
        name="device_bulk_accept",
    ),
    path(
        "devices/<int:device_pk>/apply-preview/",
        views.NSOApplyPreviewView.as_view(),
        name="device_apply_preview",
    ),
    # M13: IP auto-assignment operator action
    path(
        "devices/<int:device_pk>/auto-assign-ip/",
        views.NSOAutoAssignIPView.as_view(),
        name="device_auto_assign_ip",
    ),
    # AJAX: NSO device names for match form datalist
    path(
        "ajax/nso-device-names/<int:instance_pk>/",
        views.NSODeviceNamesView.as_view(),
        name="ajax_nso_device_names",
    ),
    # Routing state: per-row accept
    path(
        "routing/static-route-state/<int:pk>/accept/",
        views.NSOStaticRouteStateAcceptView.as_view(),
        name="routing_accept_static_route",
    ),
    path(
        "routing/isis-interface-state/<int:pk>/accept/",
        views.NSOISISInterfaceStateAcceptView.as_view(),
        name="routing_accept_isis_interface",
    ),
    path(
        "routing/isis-instance-state/<int:pk>/accept/",
        views.NSOISISInstanceStateAcceptView.as_view(),
        name="routing_accept_isis_instance",
    ),
    path(
        "routing/bgp-peer-state/<int:pk>/accept/",
        views.NSOBGPPeerStateAcceptView.as_view(),
        name="routing_accept_bgp_peer",
    ),
    path(
        "routing/route-policy-state/<int:pk>/accept/",
        views.NSORoutePolicyStateAcceptView.as_view(),
        name="routing_accept_route_policy",
    ),
    path(
        "routing/ospf-instance-state/<int:pk>/accept/",
        views.NSOOSPFInstanceStateAcceptView.as_view(),
        name="routing_accept_ospf_instance",
    ),
    path(
        "routing/ospf-interface-state/<int:pk>/accept/",
        views.NSOOSPFInterfaceStateAcceptView.as_view(),
        name="routing_accept_ospf_interface",
    ),
    path(
        "routing/redistribution-state/<int:pk>/accept/",
        views.NSORedistributionStateAcceptView.as_view(),
        name="routing_accept_redistribution",
    ),
    # Routing state: bulk accept
    path(
        "devices/<int:device_pk>/bulk-accept/static-routes/",
        views.NSOStaticRouteBulkAcceptView.as_view(),
        name="routing_bulk_accept_static_routes",
    ),
    path(
        "devices/<int:device_pk>/bulk-accept/isis-interfaces/",
        views.NSOISISInterfaceBulkAcceptView.as_view(),
        name="routing_bulk_accept_isis_interfaces",
    ),
    path(
        "devices/<int:device_pk>/bulk-accept/isis-instances/",
        views.NSOISISInstanceBulkAcceptView.as_view(),
        name="routing_bulk_accept_isis_instances",
    ),
    path(
        "devices/<int:device_pk>/bulk-accept/bgp-peers/",
        views.NSOBGPPeerBulkAcceptView.as_view(),
        name="routing_bulk_accept_bgp_peers",
    ),
    path(
        "devices/<int:device_pk>/bulk-accept/route-policy/",
        views.NSORoutePolicyBulkAcceptView.as_view(),
        name="routing_bulk_accept_route_policy",
    ),
    path(
        "devices/<int:device_pk>/bulk-accept/ospf-instances/",
        views.NSOOSPFInstanceBulkAcceptView.as_view(),
        name="routing_bulk_accept_ospf_instances",
    ),
    path(
        "devices/<int:device_pk>/bulk-accept/ospf-interfaces/",
        views.NSOOSPFInterfaceBulkAcceptView.as_view(),
        name="routing_bulk_accept_ospf_interfaces",
    ),
    path(
        "devices/<int:device_pk>/bulk-accept/redistribution/",
        views.NSORedistributionBulkAcceptView.as_view(),
        name="routing_bulk_accept_redistribution",
    ),
]
