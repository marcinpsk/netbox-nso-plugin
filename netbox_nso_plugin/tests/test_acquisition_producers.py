# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Pin classified acquisition producers and their continuation dispositions."""

import ast
import copy
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OWNED = {"accepted", "deploying", "in_sync", "apply_failed"}
EVENTS = {
    "ACCEPT",
    "ACQUIRE",
    "EDIT",
    "ACCEPTED",
    "IN_SYNC",
    "DEPLOYING",
    "APPLY_FAILED",
    "APPLY",
    "APPLY_OK",
    "APPLY_ERR",
}
TRANSITIONS = {
    "on_operator_edit",
    "on_accept",
    "_status_after_accept",
    "on_acquire",
    "_arm_static_route_generation",
    "on_apply_result",
}
MUTATIONS = {
    "create",
    "update",
    "update_or_create",
    "get_or_create",
    "bulk_create",
    "bulk_update",
    "planned_save",
    "planned_set_update",
}

# Counts include the assigned value and its transition call.
PRODUCERS = {
    ("forms.py", "_ExactOverlayFormMixin.save"): ("operator_edit", 2),
    ("forms.py", "NSOSnmpCommunityStateForm.save"): ("operator_edit", 4),
    ("forms.py", "NSOSnmpV3UserStateForm.save"): ("operator_edit", 1),
    ("ip_autoassign.py", "_assign_one_p2p_family"): ("autoassign", 2),
    ("ip_autoassign.py", "_reserve_single"): ("autoassign", 1),
    ("link_role.py", "apply_description_for_role"): ("link_role", 1),
    ("link_role.py", "enable_igp_for_role"): ("link_role", 1),
    ("ownership_planner.py", "_seed_reowned_state"): ("manifest_reown", 1),
    ("ownership_planner.py", "_seed_static_route"): ("manifest_reown", 1),
    ("signals.py", "_route_policy_acquisition_plan"): ("enclosing_accept_or_create", 2),
    ("subinterface_create.py", "create_subinterface"): ("create", 1),
    ("svi_create.py", "create_svi"): ("create", 1),
    ("views.py", "NSOAcceptAttributeView.post"): ("accept", 2),
    ("views.py", "NSOAcceptDeviceView.post"): ("accept", 1),
    ("views.py", "NSOInterfaceEditFieldView.post"): ("operator_edit", 1),
    ("views.py", "_save_owned_bfd_edit"): ("operator_edit", 2),
    ("views.py", "_save_owned_static_route_edit"): ("operator_edit", 3),
    ("views.py", "_save_owned_redistribution_edit"): ("operator_edit", 2),
    ("views.py", "_save_owned_ospf_edit"): ("operator_edit", 2),
    ("views.py", "_save_owned_isis_edit"): ("operator_edit", 2),
    ("views.py", "_save_owned_bgp_edit"): ("operator_edit", 2),
    ("views.py", "_save_owned_overlay_only_edit"): ("operator_edit", 2),
    ("views.py", "_save_owned_interface_mtu_edit"): ("operator_edit", 2),
    ("views.py", "_route_map_name_edit_operations"): ("operator_edit", 2),
    ("views.py", "_save_lacp_edit"): ("operator_edit", 6),
    ("views.py", "_save_vlan_name_edit"): ("operator_edit", 2),
    ("views.py", "NSOBulkAcceptView.post"): ("accept", 1),
    ("views.py", "RoutingStateAcceptMixin.post"): ("accept", 2),
    ("views.py", "NSOL2SapStateAcceptView.post"): ("accept", 2),
    ("views.py", "NSOLACPBundleStateAcceptView.post"): ("accept", 4),
    ("views.py", "_switchport_accept_plan"): ("accept", 2),
    ("views.py", "_ip_edit_plan_and_operations"): ("operator_edit", 2),
    ("views.py", "NSOInterfaceIPStateAcceptView.post"): ("accept", 1),
    ("views.py", "NSOStaticRouteStateAcceptView._arm_accept"): ("accept", 1),
    ("views.py", "NSOBGPPeerTemplateStateAcceptView.post"): ("accept", 2),
    ("views.py", "NSORoutePolicyStateAcceptView.post"): ("accept", 2),
    ("views.py", "RoutingBulkAcceptMixin.post"): ("accept", 1),
    ("views.py", "NSOStaticRouteBulkAcceptView._prepare_accept"): ("accept", 1),
    ("views.py", "OverlayStateAcceptMixin._post_with_renderer_writer"): ("accept", 2),
    ("views.py", "NSOInterfaceMtuStateAcceptView._accept"): ("accept", 2),
    ("views.py", "NSORoutePolicyAttachView.post"): ("create", 2),
    ("views.py", "NSOBgpPeerCreateView._create_peer"): ("create", 1),
    ("views.py", "NSOVLANAttachView.post"): ("create", 2),
    ("vlan_reconciler.py", "_vlan_repoint_plan"): ("operator_edit_from_view", 2),
}

CONTINUATIONS = {
    ("apply_state.py", "promote_current_intent"): (2, "Promote only accepted or apply_failed rows."),
    ("forms.py", "NSOInterfaceMtuStateForm.save"): (2, "Repends owned rows; unowned edits remain changed."),
    ("intent_drift.py", "_backfill_static_route_generations"): (2, "Repends deploying static routes only."),
    ("intent_state.py", "_repend_locked_rows"): (1, "Repends previously owned rows under the scope locks."),
    ("renderer_audit.py", "_repair_plan"): (3, "Repairs rows selected from owned statuses."),
    ("settlement.py", "_settle"): (1, "Correlated Apply verdicts preserve existing ownership."),
    ("views.py", "_rollback_prepare_apply"): (1, "Restores owned rows selected by the Apply attempt."),
    ("vlan_reconciler.py", "save_vlan_content"): (1, "Repends only owned attachments after a native edit."),
}

UNRESOLVED_DISPOSITIONS = {
    ("bgp_reconciler.py", "_BGPGraphPlanner.reconcile_peer"): (1, "Reconcile preserves existing ownership."),
    ("bgp_reconciler.py", "_BGPGraphPlanner.reconcile_template"): (1, "Reconcile preserves existing ownership."),
    ("bgp_reconciler.py", "_BGPGraphPlanner.plan_stale_states"): (2, "Absent rows retain or release ownership."),
    ("bgp_reconciler.py", "_BGPGraphPlanner.plan_stale_peer_state"): (2, "Absent rows retain or release ownership."),
    ("bgp_reconciler.py", "_BGPGraphPlanner.plan_invalid_peer_state"): (
        2,
        "Error handling cannot acquire an unowned row.",
    ),
    ("l2_service_reconciler.py", "_l2_service_reconcile_operations"): (2, "Reconcile preserves existing ownership."),
    ("isis_reconciler.py", "_plan_stale_states"): (2, "Absent rows retain or release ownership."),
    ("isis_reconciler.py", "_isis_reconcile_operations"): (3, "Reconcile preserves existing ownership."),
    ("subinterface_reconciler.py", "_subinterface_reconcile_operations"): (
        3,
        "Reconcile preserves existing ownership.",
    ),
    ("svi_reconciler.py", "_svi_reconcile_operations"): (3, "Reconcile preserves existing ownership."),
    ("interface_mtu_reconciler.py", "_interface_mtu_reconcile_operations"): (
        3,
        "Reconcile preserves existing ownership.",
    ),
    ("ospf_reconciler.py", "_ospf_reconcile_operations"): (4, "Reconcile preserves existing ownership."),
    ("bfd_reconciler.py", "_bfd_reconcile_operations"): (3, "Reconcile preserves existing ownership."),
    ("template_content.py", "_interface_reconcile_operations"): (
        2,
        "Adapter observations feed non-acquiring transitions.",
    ),
    ("template_content.py", "_interface_ip_reconcile_operations"): (5, "Reconcile preserves existing ownership."),
    ("template_content.py", "_snmp_reconcile_operations.retire_absent"): (
        1,
        "Absent rows retain or release ownership.",
    ),
    ("template_content.py", "_snmp_reconcile_operations"): (8, "Reconcile preserves existing ownership."),
    ("template_content.py", "_logging_reconcile_operations"): (6, "Reconcile preserves existing ownership."),
    ("template_content.py", "_static_route_reconcile_operations"): (2, "Reconcile preserves existing ownership."),
    ("redistribution_reconciler.py", "_redistribution_reconcile_operations"): (
        2,
        "Reconcile preserves existing ownership.",
    ),
    ("route_policy_reconciler.py", "_RoutePolicyGraphPlanner._state_candidate"): (
        2,
        "Reconcile preserves existing ownership.",
    ),
    ("route_policy_reconciler.py", "_RoutePolicyGraphPlanner.plan_local_state"): (
        1,
        "Reconcile preserves existing ownership.",
    ),
    ("route_policy_reconciler.py", "_RoutePolicyGraphPlanner._flag_removed"): (
        1,
        "Absent rows retain or release ownership.",
    ),
    ("route_policy_reconciler.py", "_RoutePolicyGraphPlanner.plan_resettle_conflicts"): (
        1,
        "Reconcile preserves existing ownership.",
    ),
    ("route_policy_reconciler.py", "_classification_operations"): (1, "Classification cannot acquire an unowned row."),
    ("route_policy_reconciler.py", "_resettle_operations"): (1, "Reconcile preserves existing ownership."),
    ("vlan_reconciler.py", "_vlan_reconcile_operations"): (2, "Reconcile preserves existing ownership."),
    ("vlan_reconciler.py", "_switchport_reconcile_operations"): (5, "Reconcile preserves existing ownership."),
    ("lacp_reconciler.py", "_LACPReconcilePlanner.overlay_save"): (1, "Reconcile preserves existing ownership."),
    ("lacp_reconciler.py", "_LACPReconcilePlanner.stale_overlay_save"): (1, "Absent owned rows preserve ownership."),
    ("reconcile.py", "_mark_scope_error"): (1, "Error handling cannot acquire an unowned row."),
    ("renderer_writer.py", "RendererWriter.set_update"): (1, "Forward exact values after grant validation."),
    ("ownership_querysets.py", "OwnershipQuerySet.update"): (3, "Validate status and the frozen selection before DML."),
    ("ownership_querysets.py", "OwnershipQuerySet.bulk_create"): (1, "Reject owned inserts before bulk DML."),
    ("ownership_planner.py", "maintain_manifest"): (3, "Manifest defaults contain no overlay status."),
    ("ownership_planner.py", "maintain_manifest.adopt_manifest"): (2, "Manifest metadata has no overlay status."),
    ("views.py", "_overlay_identity_plan.validate_after_acquire"): (
        1,
        "Read the persisted status for identity validation.",
    ),
    ("views.py", "NSOLoggingLevelStateUnacceptView.post"): (1, "Revert only releases ownership."),
    ("views.py", "NSOSnmpCommunityStateVerifyView.post"): (
        1,
        "Secret verification response status is not overlay status.",
    ),
    ("views.py", "NSOSnmpV3UserStateVerifyView.post"): (
        1,
        "Secret verification response status is not overlay status.",
    ),
    ("apply_settlement.py", "_record_replay_answer"): (1, "HTTP response status is not overlay status."),
    ("adapter_client.py", "list_device_generations"): (1, "Validate external generation status."),
    ("adapter_client.py", "_validated_secret_verification"): (1, "Validate external verification status."),
    ("provision_lifecycle.py", "validate_provision_evidence"): (1, "Validate external job status."),
    ("provision_lifecycle.py", "mark_provision_terminal"): (1, "Write provision lifecycle status, not overlay status."),
    ("signals.py", "_invalidate_source_admissions"): (1, "Family read admission status is not overlay status."),
    ("read_gate.py", "gated_family_run"): (1, "Observation JSON does not acquire an overlay."),
    ("filters.py", "NSOInterfaceStateFilterSet"): (1, "Declare a status filter; no row write."),
    ("tables.py", "NSOInterfaceStateTable"): (1, "Declare a status column; no row write."),
}

MIXED_CONTINUATIONS = {
    (
        "views.py",
        "_save_owned_static_route_edit",
    ): "The selected row can acquire; shared siblings must already be owned.",
    (
        "views.py",
        "_route_map_name_edit_operations",
    ): "The selected policy can acquire; renamed siblings retain their status.",
    (
        "vlan_reconciler.py",
        "_vlan_repoint_plan",
    ): "An unowned survivor acquires; owned sources retain or repend ownership.",
}


def _name(node):
    return node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else ""


def _resolve(value, bindings, seen=frozenset()):
    if isinstance(value, ast.Name) and value.id in bindings and value.id not in seen:
        return _resolve(bindings[value.id], bindings, seen | {value.id})
    if isinstance(value, ast.Subscript):
        mapping = _resolve(value.value, bindings, seen)
        if isinstance(mapping, ast.Dict):
            for key, entry in zip(mapping.keys, mapping.values, strict=True):
                if isinstance(key, ast.Constant) and ast.dump(key) == ast.dump(value.slice):
                    return _resolve(entry, bindings, seen)
    if isinstance(value, ast.Dict):
        keys, values = [], []
        for key, entry in zip(value.keys, value.values, strict=True):
            resolved = _resolve(entry, bindings, seen)
            if key is None and isinstance(resolved, ast.Dict):
                keys.extend(resolved.keys)
                values.extend(resolved.values)
            else:
                keys.append(key)
                values.append(resolved)
        return ast.Dict(keys=keys, values=values)
    if isinstance(value, ast.Call) and _name(value.func) == "dict":
        if len(value.args) == 1 and not value.keywords:
            return _resolve(value.args[0], bindings, seen)
        if not value.args:
            return _resolve(
                ast.Dict(
                    keys=[ast.Constant(k.arg) if k.arg else None for k in value.keywords],
                    values=[k.value for k in value.keywords],
                ),
                bindings,
                seen,
            )
    return value


def _may_own(value, bindings):
    value = _resolve(value, bindings)
    if isinstance(value, ast.Constant):
        return value.value in OWNED if isinstance(value.value, str) else False
    if isinstance(value, (ast.Name, ast.Attribute)):
        return _name(value) not in {"INITIAL", "IMPORTED", "CHANGED", "CONFLICT", "RESERVED", "ERROR", "UNSUPPORTED"}
    if (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Attribute)
        and _name(value.func.value) == "models"
        and _name(value.func).endswith("Field")
    ):
        return False
    if isinstance(value, ast.IfExp):
        return _may_own(value.body, bindings) or _may_own(value.orelse, bindings)
    return True


def _assignment_pairs(target, value):
    if isinstance(target, (ast.Tuple, ast.List)):
        values = (
            value.elts
            if isinstance(value, (ast.Tuple, ast.List)) and len(target.elts) == len(value.elts)
            else [None] * len(target.elts)
        )
        for nested, entry in zip(target.elts, values, strict=True):
            yield from _assignment_pairs(nested, entry)
    elif isinstance(target, ast.Starred):
        yield from _assignment_pairs(target.value, None)
    else:
        yield target, value


def _mapping_status_values(value, bindings):
    resolved = _resolve(value, bindings)
    if not isinstance(resolved, ast.Dict):
        yield None
        return
    for key, entry in zip(resolved.keys, resolved.values, strict=True):
        if key is None or not isinstance(key, ast.Constant):
            yield None
        elif key.value == "status":
            yield entry


def _status_values(node, bindings):
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
        for target in targets:
            for nested, value in _assignment_pairs(target, _resolve(node.value, bindings)):
                if _name(nested) == "status":
                    yield value
    if not isinstance(node, ast.Call):
        return
    method = _name(node.func)
    if method == "setattr" and len(node.args) == 3:
        target = _resolve(node.args[1], bindings)
        if isinstance(target, ast.Constant) and target.value == "status":
            yield node.args[2]
    if method in TRANSITIONS or method == "advance" and any(_name(child) in EVENTS for child in ast.walk(node)):
        yield node
    if method not in MUTATIONS and not method.startswith("NSO"):
        return
    for keyword in node.keywords:
        if keyword.arg == "status":
            yield keyword.value
        elif keyword.arg in {None, "defaults", "create_defaults"}:
            yield from _mapping_status_values(keyword.value, bindings)


def _bind_assignment(target, value, bindings):
    resolved = _resolve(value, bindings) if isinstance(target, (ast.Tuple, ast.List)) else value
    for nested, entry in _assignment_pairs(target, resolved):
        if isinstance(nested, ast.Name):
            bindings[nested.id] = _local_value(entry, bindings)
        elif isinstance(nested, ast.Subscript) and isinstance(nested.value, ast.Name):
            mapping = _local_value(nested.value, bindings)
            if isinstance(mapping, ast.Dict):
                mapping.keys.append(nested.slice)
                mapping.values.append(_resolve(entry, bindings))


def _local_value(value, bindings):
    if isinstance(value, ast.Name) and isinstance(bindings.get(value.id), ast.Dict):
        return bindings[value.id]
    return _resolve(value, bindings)


def _merge_bindings(bindings, branches):
    mappings = {}
    for name in set().union(*(branch.keys() for branch in branches)):
        originals = tuple(branch.get(name) for branch in branches)
        identity = (
            tuple(id(value) for value in originals) if all(isinstance(value, ast.Dict) for value in originals) else None
        )
        values = [_resolve(branch.get(name), branch) for branch in branches]
        values = [value if value is not None else ast.Name(id="unresolved_status") for value in values]
        first, *remaining = values
        for value in remaining:
            if ast.dump(first) != ast.dump(value):
                first = ast.IfExp(test=ast.Constant(True), body=first, orelse=value)
        if identity is not None:
            first = mappings.setdefault(identity, first)
        bindings[name] = first


def _copy_bindings(bindings):
    mappings = {name: value for name, value in bindings.items() if isinstance(value, ast.Dict)}
    return bindings | copy.deepcopy(mappings)


def _bind_node(node, bindings):
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
        for target in targets:
            _bind_assignment(target, node.value, bindings)
        return
    if not isinstance(node, ast.Call) or _name(node.func) != "update" or not isinstance(node.func, ast.Attribute):
        return
    mapping = _local_value(node.func.value, bindings)
    if not isinstance(mapping, ast.Dict):
        return
    for keyword in node.keywords:
        mapping.keys.append(ast.Constant(keyword.arg) if keyword.arg else None)
        mapping.values.append(_resolve(keyword.value, bindings))
    for argument in node.args:
        resolved = _resolve(argument, bindings)
        if isinstance(resolved, ast.Dict):
            mapping.keys.extend(resolved.keys)
            mapping.values.extend(resolved.values)
        else:
            mapping.keys.append(None)
            mapping.values.append(resolved)


def _walk_try(node, scope, bindings, walk):
    successful = _copy_bindings(bindings)
    partial = [_copy_bindings(bindings)]
    for statement in node.body:
        walk(statement, scope, successful)
        partial.append(_copy_bindings(successful))
    exceptional = {}
    _merge_bindings(exceptional, partial)
    for statement in node.orelse:
        walk(statement, scope, successful)
        partial.append(_copy_bindings(successful))
    branches = [successful]
    for handler in node.handlers:
        branch = _copy_bindings(exceptional)
        if handler.type is not None:
            walk(handler.type, scope, branch)
        if handler.name:
            branch[handler.name] = None
        for statement in handler.body:
            walk(statement, scope, branch)
            partial.append(_copy_bindings(branch))
        if handler.name:
            branch.pop(handler.name, None)
        branches.append(branch)
    if node.finalbody:
        branches.extend(partial)
    _merge_bindings(bindings, branches)
    for statement in node.finalbody:
        walk(statement, scope, bindings)


def _walk_match(node, scope, bindings, walk):
    walk(node.subject, scope, bindings)
    branches = []
    exhaustive = False
    for case in node.cases:
        branch = _copy_bindings(bindings)
        for pattern in ast.walk(case.pattern):
            if isinstance(pattern, (ast.MatchAs, ast.MatchStar)) and pattern.name:
                branch[pattern.name] = None
            elif isinstance(pattern, ast.MatchMapping) and pattern.rest:
                branch[pattern.rest] = None
        walk(case.pattern, scope, branch)
        if case.guard is not None:
            walk(case.guard, scope, branch)
        for statement in case.body:
            walk(statement, scope, branch)
        branches.append(branch)
        if isinstance(case.pattern, ast.MatchAs) and case.pattern.pattern is None and case.guard is None:
            exhaustive = True
    if not exhaustive:
        branches.append(_copy_bindings(bindings))
    _merge_bindings(bindings, branches)


def enumerate_producers(source):
    found = []

    def walk(node, scope=(), bindings=None):
        bindings = {} if bindings is None else bindings
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            scope = (*scope, node.name)
            bindings = _copy_bindings(bindings)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                arguments = (
                    *node.args.posonlyargs,
                    *node.args.args,
                    *node.args.kwonlyargs,
                    node.args.vararg,
                    node.args.kwarg,
                )
                for argument in filter(None, arguments):
                    bindings.pop(argument.arg, None)
        if isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While)):
            walk(getattr(node, "test", getattr(node, "iter", None)), scope, bindings)
            branches = [] if isinstance(node, ast.If) else [_copy_bindings(bindings)]
            for statements in (node.body, node.orelse):
                branch = _copy_bindings(bindings)
                if isinstance(node, (ast.For, ast.AsyncFor)):
                    _bind_assignment(node.target, None, branch)
                for statement in statements:
                    walk(statement, scope, branch)
                branches.append(branch)
            _merge_bindings(bindings, branches)
            return
        if isinstance(node, (ast.Try, ast.TryStar)):
            _walk_try(node, scope, bindings, walk)
            return
        if isinstance(node, ast.Match):
            _walk_match(node, scope, bindings, walk)
            return
        for value in _status_values(node, bindings):
            if _may_own(value, bindings):
                found.append((".".join(scope), node.lineno))
        for child in ast.iter_child_nodes(node):
            walk(child, scope, bindings)
        _bind_node(node, bindings)

    walk(ast.parse(source))
    return found


def _scopes(source):
    scopes = {}

    def walk(node, scope=()):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = (*scope, node.name)
            scopes[".".join(scope)] = node
        for child in ast.iter_child_nodes(node):
            walk(child, scope)

    walk(ast.parse(source))
    return scopes


class TestAcquisitionProducerInventory(unittest.TestCase):
    def test_classified_acquisition_set_equals_the_pinned_table(self):
        found = Counter()
        for path in ROOT.rglob("*.py"):
            if {"tests", "migrations"} & set(path.parts) or path.name == "status_machine.py":
                continue
            module = str(path.relative_to(ROOT))
            found.update((module, name) for name, _line in enumerate_producers(path.read_text(encoding="utf-8")))
        expected = {key: count for key, (_kind, count) in PRODUCERS.items()}
        expected.update({key: count for key, (count, _why) in CONTINUATIONS.items()})
        expected.update({key: count for key, (count, _why) in UNRESOLVED_DISPOSITIONS.items()})
        self.assertEqual(dict(found), expected)
        self.assertLessEqual(MIXED_CONTINUATIONS.keys(), PRODUCERS.keys())

    def test_overlay_status_mutation_shapes_are_enumerated(self):
        source = """
def acquire(row, model):
    row.status = "accepted"
    row.save()
    model.objects.update(status="in_sync")
    model.objects.update_or_create(defaults={"status": "accepted"})
    model.objects.bulk_create([NSOInterfaceState(status="accepted")])
    row.status = sm.on_operator_edit(row.status)
    row.status = sm.advance(row.status, sm.ACCEPT)
    planned_save(NSOInterfaceState(status=sm.ACCEPTED))
    planned_set_update(model.objects.all(), status="accepted")
    setattr(row, "status", "in_sync")
"""
        self.assertEqual(Counter(name for name, _line in enumerate_producers(source)), {"acquire": 11})

    def test_new_unclassified_producers_are_detected(self):
        bodies = (
            'new_status = "accepted"; row.status = new_status',
            'defaults = {"status": "accepted"}; model.objects.update_or_create(defaults=defaults)',
            'values = {"status": "in_sync"}; model.objects.create(**values)',
            'values = {"status": "accepted"}; defaults = {**values}; model.objects.create(**defaults)',
            'row.status, other = "accepted", None',
            '(other, [row.status]) = (None, ["accepted"])',
            "row.status = get_status()",
            "row.status = loadStatusField()",
            "model.objects.create(**unknown_values)",
            'values = {}; values["status"] = "accepted"; model.objects.create(**values)',
            'values = {}; values.update(status="accepted"); model.objects.create(**values)',
            'values = {}; alias = values; alias["status"] = "accepted"; model.objects.create(**values)',
            'values = {}; alias = values; alias.update(status="accepted"); model.objects.create(**values)',
            'defaults = dict(status="accepted"); model.objects.create(**defaults)',
            'new_status = "accepted"; alias = new_status; new_status = "imported"; row.status = alias',
            'values = {"status": "accepted"}; alias = values; values = {}; model.objects.create(**alias)',
            'new_status = "accepted"\n    if row.flag:\n        new_status = "imported"\n    row.status = new_status',
            'if row.flag:\n        new_status = "accepted"\n    else:\n        new_status = "imported"\n    row.status = new_status',
            'new_status = "accepted"\n    for item in []:\n        new_status = "imported"\n    row.status = new_status',
            'values = {}; alias = values\n    if row.flag:\n        pass\n    alias.update(status="accepted"); model.objects.create(**values)',
        )
        original = (ROOT / "views.py").read_text(encoding="utf-8")
        baseline = Counter(name for name, _line in enumerate_producers(original))
        for body in bodies:
            with self.subTest(body=body):
                source = "def unclassified_producer(row, model):\n    " + body + "\n"
                found = baseline + Counter(name for name, _line in enumerate_producers(source))
                self.assertNotEqual(found, baseline)
                self.assertGreater(found["unclassified_producer"], 0)

    def test_control_flow_preserves_alternative_acquiring_bindings(self):
        bodies = (
            """try:
        alias = "accepted"
    except Exception:
        alias = "imported"
    row.status = alias""",
            """alias = "accepted"
    try:
        may_raise()
        alias = "imported"
    except Exception:
        row.status = alias""",
            """try:
        alias = "accepted"
    except Exception:
        alias = "imported"
    else:
        pass
    finally:
        row.status = alias""",
            """try:
        alias = "imported"
    except Exception:
        alias = "accepted"
    else:
        alias = "imported"
    row.status = alias""",
            """match row.flag:
        case True:
            alias = "accepted"
        case _:
            alias = "imported"
    row.status = alias""",
            """alias = "accepted"
    match row.flag:
        case True:
            alias = "imported"
    row.status = alias""",
            """alias = "imported"
    match row.flag:
        case {"status": alias}:
            row.status = alias""",
            """if row.flag:
        alias = "accepted"
    else:
        alias = "imported"
    row.status = alias""",
            """try:
        values = {"status": "accepted"}
    except Exception:
        values = {"status": "imported"}
    model.objects.create(**values)""",
            """alias = "imported"
    try:
        pass
    except Exception:
        alias = "accepted"
        may_raise()
        alias = "imported"
    finally:
        row.status = alias""",
            """alias = "imported"
    try:
        pass
    except Exception:
        pass
    else:
        alias = "accepted"
        may_raise()
        alias = "imported"
    finally:
        row.status = alias""",
        )
        for body in bodies:
            with self.subTest(body=body):
                source = "def acquire(row, model):\n    " + body + "\n"
                self.assertGreater(Counter(name for name, _line in enumerate_producers(source))["acquire"], 0)

    def test_control_flow_does_not_invent_acquisition_after_unowned_overwrites(self):
        bodies = (
            """try:
        alias = "accepted"
    except Exception:
        alias = "accepted"
    finally:
        alias = "imported"
    row.status = alias""",
            """alias = "accepted"
    match row.flag:
        case True:
            alias = "imported"
        case _:
            alias = "changed"
    row.status = alias""",
            """if row.flag:
        alias = "imported"
    else:
        alias = "changed"
    row.status = alias""",
        )
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(enumerate_producers("def release(row):\n    " + body + "\n"), [])

    def test_grant_constructor_registry_is_pinned(self):
        tree = ast.parse((ROOT / "ownership_grants.py").read_text(encoding="utf-8"))
        grant = next(
            node.value
            for node in tree.body
            if isinstance(node, ast.Assign) and any(_name(target) == "GRANTS" for target in node.targets)
        )
        self.assertEqual(
            ast.literal_eval(grant.args[0]),
            {"accept", "create", "intend", "autoassign", "link_role", "operator_edit", "manifest_reown"},
        )

    def test_explicit_producers_construct_or_inherit_their_grant(self):
        inherited = {
            ("forms.py", "NSOSnmpV3UserStateForm.save"),
            ("ownership_planner.py", "_seed_reowned_state"),
            ("ownership_planner.py", "_seed_static_route"),
            ("signals.py", "_route_policy_acquisition_plan"),
            ("views.py", "NSOStaticRouteStateAcceptView._arm_accept"),
            ("views.py", "RoutingBulkAcceptMixin.post"),
            ("views.py", "NSOStaticRouteBulkAcceptView._prepare_accept"),
            ("vlan_reconciler.py", "_vlan_repoint_plan"),
        }
        for (module, scope), (kind, _count) in PRODUCERS.items():
            if (module, scope) in inherited:
                continue
            node = _scopes((ROOT / module).read_text(encoding="utf-8"))[scope]
            kinds = {
                call.args[0].value
                for call in ast.walk(node)
                if isinstance(call, ast.Call)
                and _name(call.func) == "OwnershipGrant"
                and call.args
                and isinstance(call.args[0], ast.Constant)
            }
            self.assertIn(kind, kinds, f"{module}:{scope}")

    def test_inherited_plans_require_the_enclosing_grant(self):
        for module, scope in (
            ("signals.py", "_route_policy_acquisition_plan"),
            ("vlan_reconciler.py", "_vlan_repoint_plan"),
            ("vlan_reconciler.py", "rescope_vlan"),
            ("views.py", "_write_owned_interface_mtu"),
        ):
            node = _scopes((ROOT / module).read_text(encoding="utf-8"))[scope]
            self.assertIn("grant", [arg.arg for arg in node.args.kwonlyargs])
            self.assertIsNone(node.args.kw_defaults[[arg.arg for arg in node.args.kwonlyargs].index("grant")])


if __name__ == "__main__":
    unittest.main()
