# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Compare native routing graphs with immutable typed observations."""

from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from netbox_routing.models import ISISFlexAlgo, ISISInstance, OSPFInstance, Redistribution

from netbox_nso_plugin.comparison_values import MISSING
from netbox_nso_plugin.device_differences import NOT_VISIBLE, differences
from netbox_nso_plugin.models import NSOFamilyObservation, NSOFamilyReadState
from netbox_nso_plugin.observations import observation_defaults

from ._routing_observation_case import routing_observation
from .test_gated_reconcile import _make


class TestRoutingProtocolDifferences(TestCase):
    def setUp(self):
        self.device, self.management = _make(f"protocol{uuid4().hex[:8]}")
        self.user = get_user_model().objects.create_superuser(username=f"protocol{uuid4().hex[:8]}")

    def _snapshot(self, family, document=None, coverage=None):
        state, _ = NSOFamilyReadState.objects.get_or_create(management=self.management, family=family)
        observed = routing_observation(family, document=document, coverage=coverage)
        NSOFamilyObservation.objects.update_or_create(
            read_state=state, defaults=observation_defaults(family, 1, 1, observed)
        )

    def _rows(self, scope):
        return [row for row in differences(self.management, user=self.user) if row.scope == scope]

    def _process(self, tag="CORE", **values):
        return ISISInstance.objects.create(device=self.device, process_tag=tag, **values)

    def _ospf(self, process="10", **values):
        return OSPFInstance.objects.create(
            device=self.device, name=f"OSPF-{process}", process_id=process, router_id="198.18.0.1", **values
        )

    def _redistribution(self, destination, **values):
        return Redistribution.objects.create(
            destination_type=ContentType.objects.get_for_model(destination),
            destination_id=destination.pk,
            source_protocol="static",
            **values,
        )

    def test_routing_scopes_without_read_are_unavailable(self):
        for scope in ("isis", "isis_flex_algo", "ospf", "redistribution"):
            with self.subTest(scope=scope):
                self.assertEqual([row.kind for row in self._rows(scope)], ["unavailable"])

    def test_isis_not_comparable_attribute_is_unavailable(self):
        self._process(net="49.0001.0198.0180.0001.00")
        document = {
            "processes": [
                {"process_tag": "CORE", "net": "49.0001.0198.0180.0001.00", "present": ["process_tag", "net"]}
            ],
            "interfaces": [],
            "present": ["processes", "interfaces"],
            "unprojectable": [],
        }
        self._snapshot("isis", document, {"attributes": ["net"], "not_comparable": ["net"]})
        self.assertIn(("unavailable", "net"), [(row.kind, row.attribute) for row in self._rows("isis")])

    def test_isis_match_and_mismatch(self):
        native = self._process(net="49.0001.0198.0180.0001.00")
        document = {
            "processes": [{"process_tag": "CORE", "net": native.net, "present": ["process_tag", "net"]}],
            "interfaces": [],
            "present": ["processes", "interfaces"],
            "unprojectable": [],
        }
        self._snapshot("isis", document, {"attributes": ["net"]})
        self.assertFalse(any(row.kind == "mismatch" for row in self._rows("isis")))
        document["processes"][0]["net"] = "49.0001.0198.0180.0002.00"
        self._snapshot("isis", document, {"attributes": ["net"]})
        self.assertIn(("mismatch", "net"), [(row.kind, row.attribute) for row in self._rows("isis")])

    def test_isis_authoritative_empty_and_device_only(self):
        self._process()
        document = {"processes": [], "interfaces": [], "present": ["processes", "interfaces"], "unprojectable": []}
        self._snapshot("isis", document)
        self.assertIn("netbox_only", [row.kind for row in self._rows("isis")])
        document["processes"] = [{"process_tag": "OTHER", "present": ["process_tag"]}]
        self._snapshot("isis", document)
        self.assertIn("device_only", [row.kind for row in self._rows("isis")])

    def test_isis_duplicate_process_is_ambiguous(self):
        process = {"process_tag": "CORE", "present": ["process_tag"]}
        self._snapshot(
            "isis",
            {
                "processes": [process, process],
                "interfaces": [],
                "present": ["processes", "interfaces"],
                "unprojectable": [],
            },
        )
        self.assertIn("ambiguous", [row.kind for row in self._rows("isis")])

    def test_flex_algo_match_mismatch_and_device_only(self):
        ISISFlexAlgo.objects.create(instance=self._process(), algo_id=128, metric_type="delay-metric")
        flex = {"algo_id": 128, "metric_type": "delay-metric", "present": ["algo_id", "metric_type"]}
        process = {"process_tag": "CORE", "flex_algo": [flex], "present": ["process_tag", "flex_algo"]}
        document = {
            "processes": [process],
            "interfaces": [],
            "present": ["processes", "interfaces"],
            "unprojectable": [],
        }
        self._snapshot("isis", document, {"attributes": ["metric_type"]})
        self.assertFalse(
            any(row.kind in {"mismatch", "device_only", "netbox_only"} for row in self._rows("isis_flex_algo"))
        )
        flex["metric_type"] = "igp-metric"
        self._snapshot("isis", document, {"attributes": ["metric_type"]})
        self.assertEqual([row.kind for row in self._rows("isis_flex_algo") if row.kind != "unavailable"], ["mismatch"])
        flex["algo_id"] = 129
        self._snapshot("isis", document, {"attributes": ["metric_type"]})
        self.assertEqual({row.kind for row in self._rows("isis_flex_algo")}, {"netbox_only", "device_only"})

    def test_flex_algo_omitted_or_null_nested_collection_is_unavailable(self):
        ISISFlexAlgo.objects.create(instance=self._process(), algo_id=128)
        for reported in (False, True):
            with self.subTest(reported=reported):
                process = {"process_tag": "CORE", "present": ["process_tag"]}
                if reported:
                    process.update(flex_algo=None, present=["process_tag", "flex_algo"])
                self._snapshot(
                    "isis",
                    {
                        "processes": [process],
                        "interfaces": [],
                        "present": ["processes", "interfaces"],
                        "unprojectable": [],
                    },
                )
                self.assertEqual([row.kind for row in self._rows("isis_flex_algo")], ["unavailable"])

    def test_flex_algo_duplicate_is_ambiguous(self):
        flex = {"algo_id": 128, "present": ["algo_id"]}
        process = {"process_tag": "CORE", "flex_algo": [flex, flex], "present": ["process_tag", "flex_algo"]}
        self._snapshot(
            "isis",
            {"processes": [process], "interfaces": [], "present": ["processes", "interfaces"], "unprojectable": []},
        )
        self.assertEqual([row.kind for row in self._rows("isis_flex_algo")], ["ambiguous"])

    def test_ospf_match_mismatch_and_presence(self):
        self._ospf()
        instance = {
            "process_id": "10",
            "vrf": "",
            "router_id": "198.18.0.1",
            "area": [],
            "present": ["process_id", "vrf", "router_id", "area"],
        }
        document = {
            "instances": [instance],
            "interfaces": [],
            "present": ["instances", "interfaces"],
            "unprojectable": [],
        }
        self._snapshot("ospf", document, {"attributes": ["router_id"]})
        self.assertFalse(any(row.kind == "mismatch" for row in self._rows("ospf")))
        instance["router_id"] = "198.18.0.2"
        self._snapshot("ospf", document, {"attributes": ["router_id"]})
        self.assertIn(("mismatch", "router_id"), [(row.kind, row.attribute) for row in self._rows("ospf")])
        instance["process_id"] = "20"
        self._snapshot("ospf", document, {"attributes": ["router_id"]})
        self.assertEqual({row.kind for row in self._rows("ospf")}, {"netbox_only", "device_only"})

    def test_ospf_duplicate_instance_is_ambiguous(self):
        instance = {"process_id": "10", "vrf": "", "area": [], "present": ["process_id", "vrf", "area"]}
        self._snapshot(
            "ospf",
            {
                "instances": [instance, instance],
                "interfaces": [],
                "present": ["instances", "interfaces"],
                "unprojectable": [],
            },
        )
        self.assertEqual([row.kind for row in self._rows("ospf")], ["ambiguous"])

    def test_ospf_omitted_interface_fields_match_native_defaults(self):
        from dcim.models import Interface
        from netbox_routing.models import OSPFArea, OSPFInterface

        from ._scope_observation_case import entry

        native = self._ospf()
        area = OSPFArea.objects.create(area_id="0.0.0.0", area_type="standard")
        OSPFInterface.objects.create(
            instance=native,
            interface=Interface.objects.create(device=self.device, name="Ethernet1", type="1000base-t"),
            area=area,
            passive=False,
        )
        self._snapshot(
            "ospf",
            {
                "instances": [entry(process_id="10", vrf="", area=[entry(area_id="0", area_type="standard")])],
                "interfaces": [entry(interface_name="Ethernet1", process_id="10", area_id="0")],
                "present": ["instances", "interfaces"],
                "unprojectable": [],
            },
            {"attributes": ["passive", "cost", "priority", "network_type", "auth_type"]},
        )
        self.assertFalse(any(row.kind == "mismatch" for row in self._rows("ospf")))

    def test_ospf_interface_uses_exact_instance_and_area_across_vrfs(self):
        from core.models import ObjectType
        from dcim.models import Interface
        from ipam.models import VRF
        from netbox_routing.models import OSPFArea, OSPFInterface
        from users.models import ObjectPermission

        from ._scope_observation_case import entry

        public = self._ospf(vrf=VRF.objects.create(name="PUBLIC"))
        private = OSPFInstance.objects.create(
            device=self.device,
            name="OSPF-PRIVATE",
            process_id="10",
            router_id="198.18.0.2",
            vrf=VRF.objects.create(name="PRIVATE"),
        )
        area = OSPFArea.objects.create(area_id="0.0.0.0", area_type="standard")
        port = Interface.objects.create(device=self.device, name="Ethernet1", type="1000base-t")
        OSPFInterface.objects.create(instance=public, interface=port, area=area, cost=10)
        OSPFInterface.objects.create(
            instance=private,
            interface=Interface.objects.create(device=self.device, name="Ethernet2", type="1000base-t"),
            area=area,
        )
        self.user = get_user_model().objects.create_user(username=f"ospfvrf{uuid4().hex[:8]}")
        for model in (Interface, VRF, OSPFArea, OSPFInterface, OSPFInstance):
            permission = ObjectPermission.objects.create(
                name=f"Visible {model._meta.model_name}",
                actions=["view"],
                constraints={"pk": public.pk} if model is OSPFInstance else {},
            )
            permission.object_types.add(ObjectType.objects.get_for_model(model))
            permission.users.add(self.user)
        document = {
            "instances": [
                entry(process_id="10", vrf=vrf, area=[entry(area_id="0", area_type="standard")])
                for vrf in ("PUBLIC", "PRIVATE")
            ],
            "interfaces": [],
            "present": ["instances", "interfaces"],
            "unprojectable": [],
        }
        for observed in (False, True):
            with self.subTest(observed=observed):
                document["interfaces"] = (
                    [entry(interface_name=port.name, process_id="10", area_id="0", cost=20)] if observed else []
                )
                self._snapshot("ospf", document)
                rows = self._rows("ospf")
                child_rows = [row for row in rows if row.identity == ("interface", port.name, "10")]
                if observed:
                    self.assertIn(("mismatch", "cost"), [(row.kind, row.attribute) for row in child_rows])
                else:
                    self.assertEqual([row.kind for row in child_rows], ["netbox_only"])
                self.assertNotIn("PRIVATE", repr(rows))

    def test_isis_hidden_bound_port_is_redacted_without_native_logical_interface(self):
        from core.models import ObjectType
        from dcim.models import Interface
        from users.models import ObjectPermission

        from ._scope_observation_case import entry

        hidden = Interface.objects.create(device=self.device, name="Ethernet1", type="1000base-t")
        self.user = get_user_model().objects.create_user(username=f"bound{uuid4().hex[:8]}")
        self._snapshot(
            "isis",
            {
                "processes": [],
                "interfaces": [
                    entry(
                        interface_name="Ethernet1.100",
                        af="ipv4",
                        process_tag="CORE",
                        bound_port=hidden.name,
                        setting=[entry(key="example-setting", value="example-value")],
                        level=[entry(level=2, metric=20)],
                        prefix_sid=[entry(algorithm=128, sid_index=42)],
                    )
                ],
                "present": ["processes", "interfaces"],
                "unprojectable": [],
            },
        )
        rows = self._rows("isis")
        self.assertNotIn(hidden.name, repr(rows))
        self.assertNotIn("example-value", repr(rows))
        self.assertEqual([row.kind for row in rows], ["ambiguous"] * 4)
        self.assertTrue(all(row.identity == "" for row in rows))
        permission = ObjectPermission.objects.create(name="Visible bound port", actions=["view"])
        permission.object_types.add(ObjectType.objects.get_for_model(Interface))
        permission.users.add(self.user)
        self.user = get_user_model().objects.get(pk=self.user.pk)
        rows = self._rows("isis")
        self.assertEqual([row.kind for row in rows], ["device_only"] * 4)
        self.assertIn("example-value", repr(rows))

    def test_isis_children_inherit_hidden_native_interface_dependencies(self):
        from core.models import ObjectType
        from dcim.models import Interface
        from netbox_routing.models import ISISInterface
        from users.models import ObjectPermission

        from ._scope_observation_case import entry

        process = self._process()
        port = Interface.objects.create(device=self.device, name="Ethernet1", type="1000base-t")
        ISISInterface.objects.create(instance=process, interface=port, address_family="ipv4")
        self.user = get_user_model().objects.create_user(username=f"parent{uuid4().hex[:8]}")
        for model in (Interface, ISISInstance):
            permission = ObjectPermission.objects.create(name=f"Visible {model._meta.model_name}", actions=["view"])
            permission.object_types.add(ObjectType.objects.get_for_model(model))
            permission.users.add(self.user)
        self._snapshot(
            "isis",
            {
                "processes": [],
                "interfaces": [
                    entry(
                        interface_name=port.name,
                        af="ipv4",
                        process_tag=process.process_tag,
                        setting=[entry(key="example-setting", value="example-value")],
                        level=[entry(level=2, metric=20)],
                        prefix_sid=[entry(algorithm=128, sid_index=42)],
                    )
                ],
                "present": ["processes", "interfaces"],
                "unprojectable": [],
            },
        )
        rows = self._rows("isis")
        children = [row for row in rows if row.kind != "netbox_only"]
        self.assertEqual([row.kind for row in children], ["ambiguous"] * 4)
        self.assertTrue(
            all(row.identity == "" and row.netbox_value is MISSING and row.device_value is MISSING for row in children)
        )
        self.assertNotIn(port.name, repr(children))
        self.assertNotIn("example-setting", repr(children))
        self.assertNotIn("example-value", repr(children))
        permission = ObjectPermission.objects.create(name="Visible IS-IS parent", actions=["view"])
        permission.object_types.add(ObjectType.objects.get_for_model(ISISInterface))
        permission.users.add(self.user)
        self.user = get_user_model().objects.get(pk=self.user.pk)
        rows = self._rows("isis")
        self.assertEqual(len([row for row in rows if row.kind == "device_only"]), 3)
        self.assertIn("example-setting", repr(rows))
        self.assertIn("example-value", repr(rows))

    def test_process_overlay_dependencies_restrict_flex_and_redistribution_children(self):
        from core.models import ObjectType
        from users.models import ObjectPermission

        from netbox_nso_plugin.models import NSOISISInstanceState

        from ._scope_observation_case import entry

        process = self._process()
        NSOISISInstanceState.objects.create(management=self.management, isis_instance=process)
        self.user = get_user_model().objects.create_user(username=f"overlay{uuid4().hex[:8]}")
        permission = ObjectPermission.objects.create(name="Visible IS-IS process", actions=["view"])
        permission.object_types.add(ObjectType.objects.get_for_model(ISISInstance))
        permission.users.add(self.user)
        self._snapshot(
            "isis",
            {
                "processes": [entry(process_tag=process.process_tag, flex_algo=[entry(algo_id=128, priority=123)])],
                "interfaces": [],
                "present": ["processes", "interfaces"],
                "unprojectable": [],
            },
        )
        self._snapshot(
            "redistribution",
            self._redist_document(
                [
                    entry(
                        dest_protocol="isis",
                        dest_ref=process.process_tag,
                        source_protocol="static",
                        source_ref="",
                        metric=123,
                    )
                ],
                inventory=[entry(process_tag=process.process_tag, redistribute=[])],
            ),
            self._redist_coverage(),
        )
        for scope in ("isis_flex_algo", "redistribution"):
            with self.subTest(scope=scope):
                rows = self._rows(scope)
                self.assertTrue(any(row.kind == "ambiguous" for row in rows))
                self.assertTrue(all(row.identity == "" for row in rows))
                self.assertNotIn("123", repr(rows))
        permission = ObjectPermission.objects.create(name="Visible IS-IS overlay", actions=["view"])
        permission.object_types.add(ObjectType.objects.get_for_model(NSOISISInstanceState))
        permission.users.add(self.user)
        self.user = get_user_model().objects.get(pk=self.user.pk)
        for scope in ("isis_flex_algo", "redistribution"):
            with self.subTest(scope=scope):
                self.assertTrue(any(row.kind == "device_only" for row in self._rows(scope)))

    def test_isis_duplicate_interface_bindings_keep_hidden_child_dependencies(self):
        from dcim.models import Interface

        from ._scope_observation_case import entry

        hidden = Interface.objects.create(device=self.device, name="Ethernet1", type="1000base-t")
        self.user = get_user_model().objects.create_user(username=f"bounddup{uuid4().hex[:8]}")
        interfaces = [
            entry(
                interface_name="Ethernet1.100",
                af="ipv4",
                process_tag="CORE",
                bound_port=port,
                setting=[entry(key="example-setting", value="example-value")],
                level=[entry(level=2, metric=20)],
                prefix_sid=[entry(algorithm=128, sid_index=42)],
            )
            for port in (hidden.name, None)
        ]
        self._snapshot(
            "isis",
            {
                "processes": [],
                "interfaces": interfaces,
                "present": ["processes", "interfaces"],
                "unprojectable": [],
            },
        )
        rows = self._rows("isis")
        self.assertEqual([row.kind for row in rows], ["ambiguous"] * 4)
        self.assertTrue(all(row.identity == "" for row in rows))
        self.assertNotIn(hidden.name, repr(rows))
        self.assertNotIn("example-value", repr(rows))

    def _redist_document(self, entries, *, protocol="isis", inventory=None):
        return {
            "entries": entries,
            "components": [
                {
                    "protocol": protocol,
                    "inventory": inventory if inventory is not None else [],
                    "present": ["inventory"],
                }
            ],
            "unprojectable": [],
        }

    def _redist_coverage(self, protocol="isis"):
        return {
            "attributes": ["metric", "metric_type", "route_map"],
            "components": [{"protocol": protocol, "destinations": [], "sources": []}],
        }

    def test_redistribution_match_mismatch_and_presence(self):
        self._redistribution(self._process(), metric=10)
        entry = {
            "dest_protocol": "isis",
            "dest_ref": "CORE",
            "source_protocol": "static",
            "source_ref": "",
            "metric": 10,
            "metric_type": None,
            "route_map": None,
            "present": [
                "metric",
                "metric_type",
                "route_map",
                "dest_protocol",
                "dest_ref",
                "source_protocol",
                "source_ref",
            ],
        }
        document = self._redist_document([entry])
        self._snapshot("redistribution", document, self._redist_coverage())
        self.assertFalse(any(row.kind == "mismatch" for row in self._rows("redistribution")))
        entry["metric"] = 20
        self._snapshot("redistribution", document, self._redist_coverage())
        self.assertIn(("mismatch", "metric"), [(row.kind, row.attribute) for row in self._rows("redistribution")])
        entry["dest_ref"] = "OTHER"
        self._snapshot("redistribution", document, self._redist_coverage())
        self.assertIn("device_only", [row.kind for row in self._rows("redistribution")])
        self.assertIn("netbox_only", [row.kind for row in self._rows("redistribution")])

    def test_redistribution_uncovered_component_is_unavailable(self):
        self._redistribution(self._process())
        self._snapshot("redistribution", self._redist_document([], protocol="ospf"), self._redist_coverage("ospf"))
        self.assertTrue(self._rows("redistribution"))
        self.assertTrue(all(row.kind == "unavailable" for row in self._rows("redistribution")))

    def test_redistribution_null_inventory_is_unavailable(self):
        self._redistribution(self._process())
        document = self._redist_document([])
        document["components"][0]["inventory"] = None
        self._snapshot("redistribution", document, self._redist_coverage())
        self.assertTrue(all(row.kind == "unavailable" for row in self._rows("redistribution")))

    def test_redistribution_missing_destination_collection_names_the_collection(self):
        inventories = {
            "isis": [{"process_tag": "CORE", "present": ["process_tag"]}],
            "ospf": [{"process_id": "10", "vrf": "", "present": ["process_id", "vrf"]}],
            "bgp": [
                {
                    "asn": "64512",
                    "scope": [{"vrf": "", "address_family": [{"afi": "ipv4", "present": ["afi"]}]}],
                    "present": ["asn", "scope"],
                }
            ],
        }
        for protocol, inventory in inventories.items():
            with self.subTest(protocol=protocol):
                document = self._redist_document([], protocol=protocol, inventory=inventory)
                self._snapshot("redistribution", document, self._redist_coverage(protocol))
                self.assertIn(
                    ("unavailable", "redistribute", "redistribute collection was not reported"),
                    [(row.kind, row.attribute, row.reason) for row in self._rows("redistribution")],
                )

    def test_redistribution_duplicate_is_ambiguous(self):
        entry = {
            "dest_protocol": "isis",
            "dest_ref": "CORE",
            "source_protocol": "static",
            "source_ref": "",
            "present": ["dest_protocol", "dest_ref", "source_protocol", "source_ref"],
        }
        self._snapshot("redistribution", self._redist_document([entry, entry]), self._redist_coverage())
        self.assertIn("ambiguous", [row.kind for row in self._rows("redistribution")])

    def test_redistribution_hidden_source_processes_and_vrfs_are_redacted(self):
        from core.models import ObjectType
        from django.db import transaction
        from ipam.models import VRF
        from users.models import ObjectPermission

        from ._scope_observation_case import entry

        destination = self._process()
        hidden_vrf = VRF.objects.create(name="hidden-source-vrf")
        sources = (
            ("isis", self._process(tag="hidden-isis-source"), "hidden-isis-source"),
            ("ospf", self._ospf(process="20"), "20"),
            ("isis", self._process(tag="visible-isis-source", vrf=hidden_vrf), "visible-isis-source"),
            ("ospf", self._ospf(process="30", vrf=hidden_vrf), "30"),
            ("ospf", self._ospf(process="NAMED-SOURCE"), "NAMED-SOURCE"),
        )
        self.user = get_user_model().objects.create_user(username=f"sources{uuid4().hex[:8]}")
        visible_ids = {ISISInstance: [destination.pk, sources[2][1].pk], OSPFInstance: [sources[3][1].pk]}
        for model in (ISISInstance, OSPFInstance, Redistribution):
            permission = ObjectPermission.objects.create(
                name=f"Visible {model._meta.model_name}",
                actions=["view"],
                constraints={"pk__in": visible_ids[model]} if model in visible_ids else {},
            )
            permission.object_types.add(ObjectType.objects.get_for_model(model))
            permission.users.add(self.user)
        document = self._redist_document([])
        coverage = self._redist_coverage()
        for protocol in ("ospf", "bgp"):
            document["components"].extend(self._redist_document([], protocol=protocol)["components"])
            coverage["components"].extend(self._redist_coverage(protocol)["components"])
        for protocol, source, reference in sources:
            for presence in ("both", "device_only", "netbox_only"):
                with self.subTest(protocol=protocol, source=reference, presence=presence), transaction.atomic():
                    native = self._redistribution(destination)
                    native.source_protocol, native.source_ref = protocol, reference
                    native.save(update_fields=["source_protocol", "source_ref"])
                    observed = entry(
                        dest_protocol="isis", dest_ref="CORE", source_protocol=protocol, source_ref=reference
                    )
                    if presence == "device_only":
                        native.delete()
                    document["entries"] = [] if presence == "netbox_only" else [observed]
                    self._snapshot("redistribution", document, coverage)
                    rows = self._rows("redistribution")
                    self.assertFalse(any(row.identity and reference in row.identity for row in rows))
                    self.assertNotIn(hidden_vrf.name, repr(rows))
                    if presence == "netbox_only":
                        self.assertEqual(rows, [])
                    else:
                        self.assertTrue(any(row.kind == "ambiguous" for row in rows))
                    if presence != "device_only":
                        native.delete()

        for protocol, source, reference in sources:
            source.vrf = None
            source.save(update_fields=["vrf"])
            visible_ids[type(source)].append(source.pk)
        for model, ids in visible_ids.items():
            permission = ObjectPermission.objects.get(name=f"Visible {model._meta.model_name}")
            permission.constraints = {"pk__in": ids}
            permission.save(update_fields=["constraints"])
        self.user = get_user_model().objects.get(pk=self.user.pk)
        for protocol, source, reference in sources:
            with (
                self.subTest(protocol=protocol, source=reference, presence="netbox_only_visible"),
                transaction.atomic(),
            ):
                native = self._redistribution(destination)
                native.source_protocol, native.source_ref = protocol, reference
                native.save(update_fields=["source_protocol", "source_ref"])
                document["entries"] = []
                self._snapshot("redistribution", document, coverage)
                rows = self._rows("redistribution")
                self.assertEqual([row.kind for row in rows], ["netbox_only"])
                self.assertTrue(any(reference in row.identity for row in rows))
                native.delete()

    def test_redistribution_isis_destination_with_hidden_vrf_is_redacted(self):
        from core.models import ObjectType
        from django.db import transaction
        from ipam.models import VRF
        from users.models import ObjectPermission

        from ._scope_observation_case import entry

        hidden = VRF.objects.create(name="hidden-destination-vrf")
        destination = self._process(vrf=hidden)
        self.user = get_user_model().objects.create_user(username=f"isisdest{uuid4().hex[:8]}")
        for model in (ISISInstance, Redistribution):
            permission = ObjectPermission.objects.create(name=f"Visible {model._meta.model_name}", actions=["view"])
            permission.object_types.add(ObjectType.objects.get_for_model(model))
            permission.users.add(self.user)
        observed = entry(dest_protocol="isis", dest_ref="CORE", source_protocol="static", source_ref="", metric=20)
        for presence in ("both", "device_only", "netbox_only"):
            with self.subTest(presence=presence), transaction.atomic():
                native = self._redistribution(destination, metric=10)
                if presence == "device_only":
                    native.delete()
                self._snapshot(
                    "redistribution",
                    self._redist_document([] if presence == "netbox_only" else [observed]),
                    self._redist_coverage(),
                )
                rows = self._rows("redistribution")
                self.assertNotIn(hidden.name, repr(rows))
                self.assertTrue(all(row.identity == "" for row in rows))
                self.assertEqual(
                    [row.kind for row in rows if row.kind != "unavailable"],
                    [] if presence == "netbox_only" else ["ambiguous"],
                )
                if presence != "device_only":
                    native.delete()
        permission = ObjectPermission.objects.create(name="Visible destination VRF", actions=["view"])
        permission.object_types.add(ObjectType.objects.get_for_model(VRF))
        permission.users.add(self.user)
        self.user = get_user_model().objects.get(pk=self.user.pk)
        self._redistribution(destination, metric=10)
        self._snapshot("redistribution", self._redist_document([observed]), self._redist_coverage())
        self.assertIn(("mismatch", "metric"), [(row.kind, row.attribute) for row in self._rows("redistribution")])

    def test_redistribution_hidden_destination_vrf_without_native_process_is_redacted(self):
        from ipam.models import VRF

        from ._scope_observation_case import entry

        hidden = VRF.objects.create(name="hidden-destination-vrf")
        self.user = get_user_model().objects.create_user(username=f"destvrf{uuid4().hex[:8]}")
        for protocol, reference, vrf in (
            ("ospf", "20", hidden.name),
            ("bgp", f"64512/{hidden.name}/ipv4-unicast", None),
        ):
            with self.subTest(protocol=protocol):
                observed = entry(
                    dest_protocol=protocol,
                    dest_ref=reference,
                    dest_vrf=vrf,
                    source_protocol="static",
                    source_ref="",
                )
                self._snapshot(
                    "redistribution",
                    self._redist_document([observed], protocol=protocol),
                    self._redist_coverage(protocol),
                )
                rows = self._rows("redistribution")
                self.assertNotIn(hidden.name, repr(rows))
                self.assertTrue(any(row.kind == "ambiguous" for row in rows))

    def _redist_entry(self, protocol, reference, **values):
        from ._scope_observation_case import entry

        return entry(
            dest_protocol=protocol,
            dest_ref=reference,
            source_protocol="static",
            source_ref="",
            metric=10,
            metric_type=None,
            route_map=None,
            **values,
        )

    def test_redistribution_destination_vrf_follows_the_native_destination_key(self):
        self._redistribution(self._ospf(), metric=10)
        self._redistribution(self._process(), metric=10)
        for protocol, reference, values in (
            ("ospf", "10", {"dest_vrf": None}),
            ("ospf", "10", {}),
            ("isis", "CORE", {"dest_vrf": ""}),
            ("isis", "CORE", {"dest_vrf": None}),
        ):
            with self.subTest(protocol=protocol, values=values):
                document = self._redist_document([self._redist_entry(protocol, reference, **values)], protocol=protocol)
                self._snapshot("redistribution", document, self._redist_coverage(protocol))
                rows = [row for row in self._rows("redistribution") if row.kind != "unavailable"]
                self.assertEqual([(row.kind, row.attribute) for row in rows], [])

    def test_redistribution_vrf_on_a_non_ospf_destination_is_ambiguous(self):
        self._redistribution(self._process(), metric=10)
        document = self._redist_document([self._redist_entry("isis", "CORE", dest_vrf="TENANT_A")])
        self._snapshot("redistribution", document, self._redist_coverage())
        self.assertIn(
            ("ambiguous", "invalid observed redistribution identity"),
            [(row.kind, row.reason) for row in self._rows("redistribution")],
        )

    def test_redistribution_null_ospf_destination_vrf_keeps_a_hidden_destination_redacted(self):
        from core.models import ObjectType
        from users.models import ObjectPermission

        self._redistribution(self._ospf(process="4747"), metric=10)
        self.user = get_user_model().objects.create_user(username=f"ospfdest{uuid4().hex[:8]}")
        permission = ObjectPermission.objects.create(name="Visible redistribution", actions=["view"])
        permission.object_types.add(ObjectType.objects.get_for_model(Redistribution))
        permission.users.add(self.user)
        document = self._redist_document([self._redist_entry("ospf", "4747", dest_vrf=None)], protocol="ospf")
        self._snapshot("redistribution", document, self._redist_coverage("ospf"))
        rows = self._rows("redistribution")
        compared = [(row.kind, row.reason) for row in rows if row.kind != "unavailable"]
        self.assertEqual(compared, [("ambiguous", NOT_VISIBLE)])
        self.assertNotIn("4747", repr(rows))

    def test_redistribution_bgp_source_asdot_matches_asplain(self):
        native = self._redistribution(self._process())
        native.source_protocol = "bgp"
        native.source_ref = "4200000010"
        native.save(update_fields=["source_protocol", "source_ref"])
        entry = {
            "dest_protocol": "isis",
            "dest_ref": "CORE",
            "source_protocol": "bgp",
            "source_ref": "64086.59914",
            "metric": None,
            "metric_type": None,
            "route_map": None,
            "present": [
                "dest_protocol",
                "dest_ref",
                "source_protocol",
                "source_ref",
                "metric",
                "metric_type",
                "route_map",
            ],
        }
        self._snapshot("redistribution", self._redist_document([entry]), self._redist_coverage())
        self.assertFalse(
            any(row.kind in {"mismatch", "device_only", "netbox_only"} for row in self._rows("redistribution"))
        )

    def test_flex_algo_not_comparable_attribute_is_unavailable(self):
        ISISFlexAlgo.objects.create(instance=self._process(), algo_id=128, metric_type="delay-metric")
        process = {
            "process_tag": "CORE",
            "flex_algo": [{"algo_id": 128, "metric_type": "delay-metric", "present": ["algo_id", "metric_type"]}],
            "present": ["process_tag", "flex_algo"],
        }
        document = {
            "processes": [process],
            "interfaces": [],
            "present": ["processes", "interfaces"],
            "unprojectable": [],
        }
        self._snapshot("isis", document, {"attributes": ["metric_type"], "not_comparable": ["metric_type"]})
        self.assertIn(
            ("unavailable", "metric_type"), [(row.kind, row.attribute) for row in self._rows("isis_flex_algo")]
        )

    def test_ospf_not_comparable_attribute_is_unavailable(self):
        self._ospf()
        document = {
            "instances": [
                {
                    "process_id": "10",
                    "vrf": "",
                    "router_id": "198.18.0.1",
                    "area": [],
                    "present": ["process_id", "vrf", "router_id", "area"],
                }
            ],
            "interfaces": [],
            "present": ["instances", "interfaces"],
            "unprojectable": [],
        }
        self._snapshot("ospf", document, {"attributes": ["router_id"], "not_comparable": ["router_id"]})
        self.assertIn(("unavailable", "router_id"), [(row.kind, row.attribute) for row in self._rows("ospf")])

    def test_redistribution_not_comparable_attribute_is_unavailable(self):
        self._redistribution(self._process(), metric=10)
        entry = {
            "dest_protocol": "isis",
            "dest_ref": "CORE",
            "source_protocol": "static",
            "source_ref": "",
            "metric": 10,
            "present": ["dest_protocol", "dest_ref", "source_protocol", "source_ref", "metric"],
        }
        coverage = self._redist_coverage()
        coverage["not_comparable"] = ["metric"]
        self._snapshot("redistribution", self._redist_document([entry]), coverage)
        self.assertIn(("unavailable", "metric"), [(row.kind, row.attribute) for row in self._rows("redistribution")])

    def test_redistribution_bgp_source_without_asn_is_ambiguous(self):
        entry = {
            "dest_protocol": "isis",
            "dest_ref": "CORE",
            "source_protocol": "bgp",
            "source_ref": "",
            "present": ["dest_protocol", "dest_ref", "source_protocol", "source_ref"],
        }
        self._snapshot("redistribution", self._redist_document([entry]), self._redist_coverage())
        self.assertIn("ambiguous", [row.kind for row in self._rows("redistribution")])

    def test_isis_credentials_are_unavailable_and_never_returned(self):
        self._process(area_auth_key="example-routing-key")
        process = {
            "process_tag": "CORE",
            "area_auth_key_present": True,
            "present": ["process_tag", "area_auth_key_present"],
        }
        self._snapshot(
            "isis",
            {"processes": [process], "interfaces": [], "present": ["processes", "interfaces"], "unprojectable": []},
        )
        rows = self._rows("isis")
        self.assertIn(("unavailable", "area_auth_key_present"), [(row.kind, row.attribute) for row in rows])
        self.assertNotIn("example-routing-key", repr(rows))

    def test_ospf_overlay_area_association_without_interface_compares_native_area(self):
        from netbox_routing.models import OSPFArea

        from netbox_nso_plugin.models import NSOOSPFInstanceState

        native = self._ospf()
        OSPFArea.objects.create(area_id="0.0.0.1", area_type="stub")
        NSOOSPFInstanceState.objects.create(
            management=self.management,
            ospf_instance=native,
            process_id="10",
            areas=[{"area-id": "1", "area-type": "stub"}],
        )
        instance = {
            "process_id": "10",
            "vrf": "",
            "area": [{"area_id": "1", "area_type": "stub", "present": ["area_id", "area_type"]}],
            "present": ["process_id", "vrf", "area"],
        }
        self._snapshot(
            "ospf",
            {"instances": [instance], "interfaces": [], "present": ["instances", "interfaces"], "unprojectable": []},
        )
        self.assertFalse(
            any(
                row.identity == ("instance", "10", "", "area", "0.0.0.1")
                and row.kind in {"mismatch", "netbox_only", "device_only"}
                for row in self._rows("ospf")
            )
        )

    def test_flex_unknown_collection_hides_process_name_without_native_algorithms(self):
        self._process(tag="example-hidden-process")
        self.user = get_user_model().objects.create_user(username=f"hidden{uuid4().hex[:8]}")
        process = {"process_tag": "example-hidden-process", "present": ["process_tag"]}
        self._snapshot(
            "isis",
            {"processes": [process], "interfaces": [], "present": ["processes", "interfaces"], "unprojectable": []},
        )
        rows = self._rows("isis_flex_algo")
        self.assertNotIn("example-hidden-process", repr(rows))
        self.assertTrue(any(row.kind == "ambiguous" for row in rows))

    def test_redistribution_unknown_sources_hide_destination_name_without_native_entries(self):
        self._process(tag="example-hidden-process")
        self.user = get_user_model().objects.create_user(username=f"hidden{uuid4().hex[:8]}")
        inventory = [{"process_tag": "example-hidden-process", "present": ["process_tag"]}]
        self._snapshot("redistribution", self._redist_document([], inventory=inventory), self._redist_coverage())
        rows = self._rows("redistribution")
        self.assertNotIn("example-hidden-process", repr(rows))
        self.assertTrue(any(row.kind == "ambiguous" for row in rows))

    def test_malformed_ospf_overlay_area_association_is_ambiguous(self):
        from netbox_nso_plugin.models import NSOOSPFInstanceState

        native = self._ospf()
        overlay = NSOOSPFInstanceState.objects.create(
            management=self.management, ospf_instance=native, process_id="10", areas=[None]
        )
        instance = {"process_id": "10", "vrf": "", "area": [], "present": ["process_id", "vrf", "area"]}
        for areas in ([None], {"unexpected": "shape"}):
            with self.subTest(areas=areas):
                overlay.areas = areas
                overlay.save(update_fields=["areas"])
                self._snapshot(
                    "ospf",
                    {
                        "instances": [instance],
                        "interfaces": [],
                        "present": ["instances", "interfaces"],
                        "unprojectable": [],
                    },
                )
                self.assertTrue(
                    any(
                        row.kind == "ambiguous" and row.reason == "invalid native OSPF area association"
                        for row in self._rows("ospf")
                    )
                )

    def test_arcos_isis_omitted_boolean_defaults_match_existing_reconciler(self):
        from dcim.models import Platform

        from netbox_nso_plugin.models import NSOPlatformNedMapping

        platform = Platform.objects.create(name="example-platform", slug="example-platform")
        NSOPlatformNedMapping.objects.create(platform=platform, ned_id="arcos-example")
        self.device.platform = platform
        self.device.save(update_fields=["platform"])
        self.management.device = self.device
        self._process(overload_bit=False, ignore_attached_bit=False, suppress_attached_bit=False)
        process = {"process_tag": "CORE", "present": ["process_tag"]}
        self._snapshot(
            "isis",
            {"processes": [process], "interfaces": [], "present": ["processes", "interfaces"], "unprojectable": []},
        )
        self.assertFalse(
            any(
                row.kind == "mismatch"
                and row.attribute in {"overload_bit", "ignore_attached_bit", "suppress_attached_bit"}
                for row in self._rows("isis")
            )
        )

    def test_isis_omitted_process_flags_match_false_and_detect_true_intent(self):
        native = self._process(microloop_avoidance=False, overload_bit=False)
        process = {"process_tag": "CORE", "present": ["process_tag"]}
        self._snapshot(
            "isis",
            {"processes": [process], "interfaces": [], "present": ["processes", "interfaces"], "unprojectable": []},
            {"attributes": ["microloop_avoidance", "overload_bit"]},
        )
        self.assertFalse(any(row.kind == "mismatch" for row in self._rows("isis")))
        native.microloop_avoidance = True
        native.save(update_fields=["microloop_avoidance"])
        self.assertIn(("mismatch", "microloop_avoidance"), [(row.kind, row.attribute) for row in self._rows("isis")])

    def test_isis_locator_equivalent_ipv6_spelling_matches_native_field(self):
        from netbox_routing.models import ISISSRv6Locator

        ISISSRv6Locator.objects.create(instance=self._process(), name="example-locator", prefix="2001:db8::/64")
        locator = {"name": "example-locator", "prefix": "2001:0db8:0000:0000::/64", "present": ["name", "prefix"]}
        process = {"process_tag": "CORE", "srv6_locator": [locator], "present": ["process_tag", "srv6_locator"]}
        self._snapshot(
            "isis",
            {"processes": [process], "interfaces": [], "present": ["processes", "interfaces"], "unprojectable": []},
            {"attributes": ["prefix"]},
        )
        self.assertFalse(any(row.kind == "mismatch" and row.attribute == "prefix" for row in self._rows("isis")))

    def test_isis_invalid_locator_prefix_is_ambiguous(self):
        locator = {"name": "example-locator", "prefix": "invalid-prefix", "present": ["name", "prefix"]}
        process = {"process_tag": "CORE", "srv6_locator": [locator], "present": ["process_tag", "srv6_locator"]}
        self._snapshot(
            "isis",
            {"processes": [process], "interfaces": [], "present": ["processes", "interfaces"], "unprojectable": []},
            {"attributes": ["prefix"]},
        )
        self.assertTrue(
            any(
                row.kind == "ambiguous" and row.reason == "observed routing values cannot be projected"
                for row in self._rows("isis")
            )
        )

    def test_invalid_bgp_inventory_does_not_claim_native_redistribution_absence(self):
        from ipam.models import ASN, RIR
        from netbox_routing.models import BGPAddressFamily, BGPRouter, BGPScope

        registry = RIR.objects.create(name="example-registry", slug="example-registry")
        asn = ASN.objects.create(asn=64512, rir=registry)
        router = BGPRouter.objects.create(
            asn=asn,
            assigned_object_type=ContentType.objects.get_for_model(self.device),
            assigned_object_id=self.device.pk,
        )
        family = BGPAddressFamily.objects.create(
            scope=BGPScope.objects.create(router=router), address_family="ipv4-unicast"
        )
        self._redistribution(family)
        document = {
            "entries": [],
            "components": [
                {
                    "protocol": "bgp",
                    "inventory": [{"asn": " 64512", "scope": [], "present": ["asn", "scope"]}],
                    "present": ["inventory"],
                }
            ],
            "unprojectable": [],
        }
        coverage = self._redist_coverage("bgp")
        self._snapshot("redistribution", document, coverage)
        rows = self._rows("redistribution")
        self.assertTrue(rows)
        self.assertTrue(all(row.kind == "unavailable" for row in rows))
        self.assertIn("redistribution inventory has an invalid AS number", [row.reason for row in rows])
