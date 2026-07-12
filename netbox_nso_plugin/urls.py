# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from django.urls import path
from netbox.views.generic.feature_views import ObjectChangeLogView, ObjectJournalView

from . import views
from .models import (
    NSODeviceManagement,
    NSOInstance,
    NSOInterfaceState,
    NSOLinkRole,
    NSOLinkRoleAssignment,
    NSOPlatformNedMapping,
)

app_name = "netbox_nso_plugin"

urlpatterns = [
    # Adapter Connection (singleton)
    path("adapter-connection/", views.AdapterConnectionEditView.as_view(), name="adapterconnection"),
    # Failover Settings (singleton)
    path("failover-settings/", views.NSOFailoverSettingsEditView.as_view(), name="nsofailoversettings"),
    # Vault Settings (singleton)
    path("vault-settings/", views.NSOVaultSettingsEditView.as_view(), name="nsovaultsettings"),
    # NSO Instances
    path("instances/", views.NSOInstanceListView.as_view(), name="nsoinstance_list"),
    path("instances/add/", views.NSOInstanceEditView.as_view(), name="nsoinstance_add"),
    path("instances/delete/", views.NSOInstanceBulkDeleteView.as_view(), name="nsoinstance_bulk_delete"),
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
    # Link roles (configurable provisioning catalog)
    path("link-roles/", views.NSOLinkRoleListView.as_view(), name="nsolinkrole_list"),
    path("link-roles/add/", views.NSOLinkRoleEditView.as_view(), name="nsolinkrole_add"),
    path("link-roles/delete/", views.NSOLinkRoleBulkDeleteView.as_view(), name="nsolinkrole_bulk_delete"),
    path("link-roles/<int:pk>/", views.NSOLinkRoleView.as_view(), name="nsolinkrole"),
    path("link-roles/<int:pk>/edit/", views.NSOLinkRoleEditView.as_view(), name="nsolinkrole_edit"),
    path("link-roles/<int:pk>/delete/", views.NSOLinkRoleDeleteView.as_view(), name="nsolinkrole_delete"),
    path(
        "link-roles/<int:pk>/changelog/",
        ObjectChangeLogView.as_view(),
        name="nsolinkrole_changelog",
        kwargs={"model": NSOLinkRole},
    ),
    path(
        "link-roles/<int:pk>/journal/",
        ObjectJournalView.as_view(),
        name="nsolinkrole_journal",
        kwargs={"model": NSOLinkRole},
    ),
    # Link-role assignments (role ← cable or interface)
    path("link-role-assignments/", views.NSOLinkRoleAssignmentListView.as_view(), name="nsolinkroleassignment_list"),
    path(
        "link-role-assignments/add/",
        views.NSOLinkRoleAssignmentEditView.as_view(),
        name="nsolinkroleassignment_add",
    ),
    path(
        "link-role-assignments/delete/",
        views.NSOLinkRoleAssignmentBulkDeleteView.as_view(),
        name="nsolinkroleassignment_bulk_delete",
    ),
    path(
        "link-role-assignments/<int:pk>/",
        views.NSOLinkRoleAssignmentView.as_view(),
        name="nsolinkroleassignment",
    ),
    path(
        "link-role-assignments/<int:pk>/edit/",
        views.NSOLinkRoleAssignmentEditView.as_view(),
        name="nsolinkroleassignment_edit",
    ),
    path(
        "link-role-assignments/<int:pk>/delete/",
        views.NSOLinkRoleAssignmentDeleteView.as_view(),
        name="nsolinkroleassignment_delete",
    ),
    path(
        "link-role-assignments/<int:pk>/changelog/",
        ObjectChangeLogView.as_view(),
        name="nsolinkroleassignment_changelog",
        kwargs={"model": NSOLinkRoleAssignment},
    ),
    # Onboarding dashboard (tabs) + onboard action
    path("onboarding/", views.NSOOnboardingDashboardView.as_view(), name="onboarding_dashboard"),
    path("onboard/", views.NSOOnboardView.as_view(), name="onboard"),
    path("onboard-status/<int:pk>/", views.NSOOnboardStatusView.as_view(), name="onboard_status"),
    path("manage/", views.NSOQuickManageView.as_view(), name="quick_manage"),
    # Platform → NED mappings (onboarding)
    path("ned-mappings/", views.NSOPlatformNedMappingListView.as_view(), name="nsoplatformnedmapping_list"),
    path("ned-mappings/add/", views.NSOPlatformNedMappingEditView.as_view(), name="nsoplatformnedmapping_add"),
    path(
        "ned-mappings/delete/",
        views.NSOPlatformNedMappingBulkDeleteView.as_view(),
        name="nsoplatformnedmapping_bulk_delete",
    ),
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
    # Device NSO tab — live category header counts (JSON; refreshes stale badges post-Sync)
    path(
        "devices/<int:device_pk>/category-counts/",
        views.NSOCategoryCountsView.as_view(),
        name="device_nso_category_counts",
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
    path(
        "device-management/delete/",
        views.NSODeviceManagementBulkDeleteView.as_view(),
        name="nsodevicemanagement_bulk_delete",
    ),
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
    # Re-sync orphaned adapter intent (clears adapter↔NetBox split-brain)
    path(
        "device-management/<int:pk>/intent-resync/",
        views.NSOIntentResyncView.as_view(),
        name="nsodevicemanagement_intent_resync",
    ),
    # Retry linking a managed device to the adapter after a failed onboard/scope/sync
    path(
        "device-management/<int:pk>/link-retry/",
        views.NSOAdapterLinkRetryView.as_view(),
        name="nsodevicemanagement_link_retry",
    ),
    # Operator override for a collateral-blocked removal (guard skipped, reviewed orphans flushed)
    path(
        "device-management/<int:pk>/force-removal/",
        views.NSOForceRemovalView.as_view(),
        name="nsodevicemanagement_force_removal",
    ),
    # Adapter job status (JSON, for client-side polling)
    path("jobs/<int:job_id>/status/", views.NSOJobStatusView.as_view(), name="nsojob_status"),
    # Device job-activity summary (JSON: running + last finished) for the tab status strip
    path("devices/<int:pk>/jobs/", views.NSODeviceJobsView.as_view(), name="device_nso_jobs"),
    # Interface State CRUD (read-only list + detail; delete allowed for cleanup)
    path("interface-state/", views.NSOInterfaceStateListView.as_view(), name="nsointerfacestate_list"),
    path(
        "interface-state/delete/",
        views.NSOInterfaceStateBulkDeleteView.as_view(),
        name="nsointerfacestate_bulk_delete",
    ),
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
        "interface-ip-state/<int:pk>/accept/",
        views.NSOInterfaceIPStateAcceptView.as_view(),
        name="nsointerfaceipstate_accept",
    ),
    path(
        "interface-state/<int:pk>/edit-field/",
        views.NSOInterfaceEditFieldView.as_view(),
        name="nsointerfacestate_edit_field",
    ),
    path(
        "overlay/<str:key>/<int:pk>/edit-field/",
        views.NSOOverlayFieldEditView.as_view(),
        name="overlay_field_edit",
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
    # IP auto-assignment operator action
    path(
        "devices/<int:device_pk>/auto-assign-ip/",
        views.NSOAutoAssignIPView.as_view(),
        name="device_auto_assign_ip",
    ),
    # Link-role provisioning operator action
    path(
        "devices/<int:device_pk>/provision-link-role/",
        views.NSOProvisionLinkRoleView.as_view(),
        name="device_provision_link_role",
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
    # L2 (Nokia SAP) state accept —
    path(
        "l2/sap-state/<int:pk>/accept/",
        views.NSOL2SapStateAcceptView.as_view(),
        name="l2_accept_sap",
    ),
    # LACP bundle state accept → apply —
    path(
        "lacp/bundle-state/<int:pk>/accept/",
        views.NSOLACPBundleStateAcceptView.as_view(),
        name="lacp_accept_bundle",
    ),
    # Switchport state accept → apply —
    path(
        "switchport/state/<int:pk>/accept/",
        views.NSOSwitchportStateAcceptView.as_view(),
        name="switchport_accept",
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
        "routing/bgp-peer-template-state/<int:pk>/accept/",
        views.NSOBGPPeerTemplateStateAcceptView.as_view(),
        name="routing_accept_bgp_peer_template",
    ),
    path(
        "routing/route-policy-state/<int:pk>/accept/",
        views.NSORoutePolicyStateAcceptView.as_view(),
        name="routing_accept_route_policy",
    ),
    # Shared-object versions + re-point (pick which device's version NetBox mirrors)
    path(
        "routing/route-policy-state/<int:pk>/versions/",
        views.NSORoutePolicyVersionsView.as_view(),
        name="routing_route_policy_versions",
    ),
    path(
        "routing/route-policy-state/<int:pk>/materialize/",
        views.NSORoutePolicyMaterializeView.as_view(),
        name="routing_materialize_route_policy",
    ),
    path(
        "routing/route-policy-state/<int:pk>/classify/",
        views.NSORoutePolicyClassifyView.as_view(),
        name="routing_classify_route_policy",
    ),
    path(
        "devices/<int:device_pk>/route-policy/classify-bulk/",
        views.NSORoutePolicyClassifyBulkView.as_view(),
        name="routing_classify_bulk_route_policy",
    ),
    # Drift delta: device capture vs the materialised NetBox object.
    path(
        "routing/route-policy-state/<int:pk>/diff/",
        views.NSORoutePolicyDiffView.as_view(),
        name="routing_route_policy_diff",
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
    path(
        "routing/redistribution-state/<int:pk>/diff/",
        views.NSORedistributionDiffView.as_view(),
        name="routing_redistribution_diff",
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
    # ── SNMP / Logging overlay edit + accept (operator modify → accept → push) ──
    path(
        "snmp/community-state/<int:pk>/edit/",
        views.NSOSnmpCommunityStateEditView.as_view(),
        name="nsosnmpcommunitystate_edit",
    ),
    path(
        "snmp/community-state/<int:pk>/accept/",
        views.NSOSnmpCommunityStateAcceptView.as_view(),
        name="snmp_accept_community",
    ),
    path(
        "snmp/community-state/<int:pk>/verify-secret/",
        views.NSOSnmpCommunityStateVerifyView.as_view(),
        name="snmp_verify_community_secret",
    ),
    path(
        "snmp/community-state/<int:pk>/harvest-secret/",
        views.NSOSnmpCommunityStateHarvestView.as_view(),
        name="snmp_harvest_community_secret",
    ),
    path(
        "snmp/v3-user-state/<int:pk>/edit/",
        views.NSOSnmpV3UserStateEditView.as_view(),
        name="nsosnmpv3userstate_edit",
    ),
    path(
        "snmp/v3-user-state/<int:pk>/accept/",
        views.NSOSnmpV3UserStateAcceptView.as_view(),
        name="snmp_accept_v3_user",
    ),
    path(
        "snmp/v3-user-state/<int:pk>/verify-secret/",
        views.NSOSnmpV3UserStateVerifyView.as_view(),
        name="snmp_verify_v3_user_secret",
    ),
    path(
        "snmp/host-state/<int:pk>/edit/",
        views.NSOSnmpHostStateEditView.as_view(),
        name="nsosnmphoststate_edit",
    ),
    path(
        "snmp/host-state/<int:pk>/accept/",
        views.NSOSnmpHostStateAcceptView.as_view(),
        name="snmp_accept_host",
    ),
    path(
        "snmp/system-info-state/<int:pk>/edit/",
        views.NSOSnmpSystemInfoStateEditView.as_view(),
        name="nsosnmpsysteminfostate_edit",
    ),
    path(
        "snmp/system-info-state/<int:pk>/accept/",
        views.NSOSnmpSystemInfoStateAcceptView.as_view(),
        name="snmp_accept_system_info",
    ),
    path(
        "logging/host-state/<int:pk>/edit/",
        views.NSOLoggingHostStateEditView.as_view(),
        name="nsologginghoststate_edit",
    ),
    path(
        "logging/host-state/<int:pk>/accept/",
        views.NSOLoggingHostStateAcceptView.as_view(),
        name="logging_accept_host",
    ),
    path(
        "svi/state/<int:pk>/accept/",
        views.NSOSVIStateAcceptView.as_view(),
        name="svi_accept",
    ),
    path(
        "subinterface/state/<int:pk>/accept/",
        views.NSOSubinterfaceStateAcceptView.as_view(),
        name="subinterface_accept",
    ),
    path(
        "interface-mtu/state/<int:pk>/accept/",
        views.NSOInterfaceMtuStateAcceptView.as_view(),
        name="interface_mtu_accept",
    ),
    path(
        "interface-mtu/state/<int:pk>/edit/",
        views.NSOInterfaceMtuStateEditView.as_view(),
        name="nsointerfacemtustate_edit",
    ),
    path(
        "vlan/state/<int:pk>/accept/",
        views.NSOVLANStateAcceptView.as_view(),
        name="vlan_accept",
    ),
    path(
        "vlan/state/<int:pk>/rescope/",
        views.NSOVLANRescopeView.as_view(),
        name="vlan_rescope",
    ),
    path(
        "device/<int:device_pk>/vlan/attach/",
        views.NSOVLANAttachView.as_view(),
        name="vlan_attach",
    ),
    path(
        "device/<int:device_pk>/bgp/add-peer/",
        views.NSOBgpPeerCreateView.as_view(),
        name="bgp_peer_add",
    ),
    path(
        "device/<int:device_pk>/route-policy/attach/",
        views.NSORoutePolicyAttachView.as_view(),
        name="route_policy_attach",
    ),
    path(
        "device/<int:device_pk>/route-policy/capabilities/",
        views.NSORoutePolicyCapabilityView.as_view(),
        name="route_policy_capabilities",
    ),
    path(
        "bfd/state/<int:pk>/accept/",
        views.NSOBFDInterfaceStateAcceptView.as_view(),
        name="bfd_accept",
    ),
]
