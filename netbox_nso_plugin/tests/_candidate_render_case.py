# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Real native graphs and exact writer operations for render parity."""

from datetime import UTC, datetime

from dcim.models import Device, Interface
from django.contrib.contenttypes.models import ContentType
from ipam.models import ASN, RIR, VLAN, IPAddress, VLANGroup
from netbox_routing.models import (
    BGPAddressFamily,
    BGPPeer,
    BGPPeerAddressFamily,
    BGPRouter,
    BGPScope,
    ISISFlexAlgo,
    ISISInstance,
    ISISInterface,
    ISISLevel,
    OSPFArea,
    OSPFInstance,
    OSPFInterface,
    PrefixList,
    Redistribution,
    StaticRoute,
)

from netbox_nso_plugin import models
from netbox_nso_plugin.ownership_grants import OwnershipGrant
from netbox_nso_plugin.renderer_writer import (
    RendererMutationPlan,
    planned_m2m_set,
    planned_save,
    renderer_mirror_writes,
    renderer_writes,
)

from ._outbox_case import without_commit_drain

ACCEPTED_AT = datetime(2026, 11, 1, tzinfo=UTC)


def render_candidates(device, management, index):
    if not management.manage_description:
        management.manage_description = True
        with without_commit_drain():
            management.save(update_fields=("manage_description",))
    interface = Interface.objects.create(
        device=device, name=f"Ethernet{index}", type="1000base-t", description=f"link-{index}", mtu=1500, mode="tagged"
    )
    lag = Interface.objects.create(device=device, name=f"Port-channel{index}", type="lag")
    member = Interface.objects.create(device=device, name=f"Ethernet{index}/1", type="1000base-t", lag=lag)
    group, _ = VLANGroup.objects.get_or_create(slug=f"nso-{device.pk}", defaults={"name": "Render VLANs"})
    vlan = VLAN.objects.create(group=group, vid=100 + index, name=f"render-vlan-{index}")
    IPAddress.objects.create(
        address=f"198.18.{index}.1/24",
        assigned_object_type=ContentType.objects.get_for_model(Interface),
        assigned_object_id=interface.pk,
    )
    route = StaticRoute.objects.create(prefix=f"198.18.{index}.0/24", next_hop="198.18.0.1", metric=index)
    route.devices.add(device)
    isis = ISISInstance.objects.create(device=device, process_tag=f"CORE{index}", net="49.0001.0001.0001.0001.00")
    isis_interface = ISISInterface.objects.create(instance=isis, interface=interface, address_family="ipv4")
    ISISLevel.objects.create(instance=isis, level=2, default_metric=50)
    flex = ISISFlexAlgo.objects.create(instance=isis, algo_id=128 + index, priority=100)
    ospf = OSPFInstance.objects.create(
        device=device, name=f"ospf-{index}", process_id=str(index), router_id=f"198.18.0.{index}"
    )
    area = OSPFArea.objects.create(area_id="0.0.0.0")
    OSPFInterface.objects.create(instance=ospf, interface=interface, area=area)
    rir, _ = RIR.objects.get_or_create(name="Render private", slug="render-private")
    asn = ASN.objects.create(asn=64512 + index, rir=rir)
    router = BGPRouter.objects.create(
        assigned_object_type=ContentType.objects.get_for_model(Device),
        assigned_object_id=device.pk,
        asn=asn,
        name=f"render-router-{index}",
        router_id=f"198.18.0.{index}",
    )
    scope = BGPScope.objects.create(router=router)
    af = BGPAddressFamily.objects.create(scope=scope, address_family="ipv4-unicast")
    peer = BGPPeer.objects.create(scope=scope, peer=IPAddress.objects.create(address=f"198.18.{index}.2/32"), name=None)
    BGPPeerAddressFamily.objects.create(
        assigned_object_type=ContentType.objects.get_for_model(BGPPeer), assigned_object_id=peer.pk, address_family=af
    )
    policy = PrefixList.objects.create(name=f"render-prefixes-{index}", family=4)
    common = {"management": management, "status": "accepted", "accepted_at": ACCEPTED_AT}
    rows = (
        models.NSOInterfaceState(
            interface=interface, attribute="description", status="accepted", accepted_at=ACCEPTED_AT
        ),
        models.NSOInterfaceIPState(
            interface=interface, address=f"198.18.{index}.1/24", status="accepted", accepted_at=ACCEPTED_AT
        ),
        models.NSOInterfaceMtuState(interface=interface, l2_mtu=1500, **common),
        models.NSOVLANState(vlan=vlan, **common),
        models.NSOStaticRouteState(static_route=route, **common),
        models.NSOISISInstanceState(process_tag=isis.process_tag, isis_instance=isis, net=isis.net, **common),
        models.NSOISISInterfaceState(
            interface=interface, af="ipv4", process_tag=isis.process_tag, isis_interface=isis_interface, **common
        ),
        models.NSOISISFlexAlgoState(
            process_tag=isis.process_tag, algo_id=flex.algo_id, isis_flex_algo=flex, priority=100, **common
        ),
        models.NSOOSPFInstanceState(process_id=str(index), ospf_instance=ospf, router_id=f"198.18.0.{index}", **common),
        models.NSOOSPFInterfaceState(interface=interface, process_id=str(index), area_id="0.0.0.0", **common),
        models.NSOBGPRouterState(asn_str=str(asn.asn), bgp_router=router, router_id=str(router.router_id), **common),
        models.NSOBGPPeerState(asn_str=str(asn.asn), bgp_peer=peer, peer_address_str=f"198.18.{index}.2", **common),
        models.NSORoutePolicyState(
            family="prefix_list",
            object_name=policy.name,
            content_type=ContentType.objects.get_for_model(PrefixList),
            object_id=policy.pk,
            **common,
        ),
        models.NSOLACPBundleState(interface=lag, lag_id=index, timer="fast", observed_members=[member.name], **common),
        models.NSOLACPMemberState(interface=member, mode="active", port_priority=100, **common),
        models.NSOSwitchportState(interface=interface, mode="tagged", **common),
    )
    redistribution = []
    for protocol, destination, ref in (
        ("isis", isis, isis.process_tag),
        ("ospf", ospf, str(index)),
        ("bgp", af, f"{asn.asn}//ipv4-unicast"),
    ):
        native = Redistribution.objects.create(
            destination_type=ContentType.objects.get_for_model(type(destination)),
            destination_id=destination.pk,
            source_protocol="connected",
            source_ref="",
            metric=index,
        )
        redistribution.append(
            models.NSORedistributionState(
                dest_protocol=protocol, dest_ref=ref, source_protocol="connected", redistribution=native, **common
            )
        )
    return (*rows, *redistribution), (planned_m2m_set(rows[-1], "tagged_vlans", (vlan,)),)


def persist_candidates(rows, relations=()):
    saves = tuple(
        planned_save(row, force_insert=row.pk is None, natural_key=tuple(row._meta.unique_together[0])) for row in rows
    )
    plan = RendererMutationPlan.build(
        saves=saves, m2m_writes=relations, grant=OwnershipGrant("create"), settles_deploying=False
    )
    mutation = renderer_writes if plan.changes_content else renderer_mirror_writes
    with without_commit_drain(), mutation(plan) as writer:
        for row in rows:
            writer.save(row, force_insert=row.pk is None)
        for relation in relations:
            writer.m2m_set(relation.instance, relation.field_name, relation.related)
