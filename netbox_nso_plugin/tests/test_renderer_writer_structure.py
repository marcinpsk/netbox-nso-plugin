# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Structural backstops for renderer-input writes in production modules.

D-21 asks one question of the tree: does any production code mutate a registered
renderer-input model outside the reviewed writer seam? The guard answers it per CALL SITE,
because module granularity cannot: an allow-list of file names exempts every future write
in an already-listed file, and views, signals, forms and ``intent_state`` are the files
that write the most.

A site is flagged only when its target model resolves STATICALLY — from an imported model
symbol, from ``apps.get_model("<literal>")``, or from a local name bound to either. That is
the honest limit of an AST guard: ``self.model_class`` and a model handed in as an argument
resolve to nothing and are not reported. Raw ``cursor.execute`` DML is read the same way,
against each registered model's own ``db_table``.
"""

from __future__ import annotations

import ast
import dataclasses
import re
from pathlib import Path

from django.test import SimpleTestCase

_MUTATION_METHODS = frozenset(
    {
        "add",
        "bulk_create",
        "bulk_update",
        "clear",
        "create",
        "delete",
        "get_or_create",
        "remove",
        "save",
        "set",
        "update",
        "update_or_create",
    }
)
#: ``copy(row)`` keeps the row's model, and the repair planner builds its candidates that way.
_COPY_HELPERS = frozenset({"copy", "deepcopy"})
_FUNCTION_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef)
_DML_TARGET = re.compile(r"\b(?:insert\s+into|update|delete\s+from)\s+\"?([a-z0-9_]+)\"?", re.IGNORECASE)
#: A binding may name a later one (``rows = Model.objects…`` then ``row = rows[0]``), and the
#: scan is flow-insensitive, so the scope is re-read until those chains have settled.
_BINDING_PASSES = 3

#: Every statically resolvable mutation of a registered renderer-input model, reviewed for
#: #1627 P4. Keyed by (module, enclosing qualified name, mutation expression): a NEW write in
#: an already-listed module is a new key and fails the guard until it is reviewed and added.
_REVIEWED_MUTATION_SITES = frozenset(
    {
        ("drain.py", "_stamp_last_acked", "NSOStaticRouteState.objects.bulk_update"),
        ("drain.py", "clear_acknowledged_lineage", "NSOStaticRouteState.objects.exclude().update"),
        ("link_role.py", "apply_description_for_role", "NSOInterfaceState.objects.update_or_create"),
        ("link_role.py", "enable_igp_for_role", "NSOISISInterfaceState.objects.update_or_create"),
        ("link_role.py", "enable_igp_for_role", "NSOOSPFInterfaceState.objects.update_or_create"),
        ("onboarding.py", "onboard_candidate", "NSOPlatformNedMapping.objects.get_or_create"),
        ("template_content.py", "_reconcile_lag_topology", "stale.save"),
    }
)


@dataclasses.dataclass(frozen=True)
class _Registry:
    """The registered renderer inputs, indexed the three ways the scan resolves them."""

    labels: frozenset
    #: Imported model class name -> label; an ambiguous name resolves to nothing.
    names: dict
    #: ``db_table`` -> label, and its inverse, for the raw-DML arm.
    tables: dict
    db_tables: dict


@dataclasses.dataclass(frozen=True)
class _Site:
    """One mutation call site and the registered model it was resolved to."""

    module: str
    function: str
    expression: str
    label: str
    lineno: int

    @property
    def key(self) -> tuple:
        return (self.module, self.function, self.expression)


def _registry() -> _Registry:
    from netbox_nso_plugin.intent_state import renderer_input_specs

    specs = renderer_input_specs()
    names: dict[str, str] = {}
    ambiguous = set()
    for label, spec in specs.items():
        name = spec.model.__name__
        if names.setdefault(name, label) != label:
            ambiguous.add(name)
    for name in ambiguous:
        del names[name]
    db_tables = {label: spec.model._meta.db_table for label, spec in specs.items()}
    tables = {table: label for label, table in db_tables.items()}
    return _Registry(labels=frozenset(specs), names=names, tables=tables, db_tables=db_tables)


def _own_nodes(scope):
    """Every descendant of *scope* that a nested function scope does not own."""
    for child in ast.iter_child_nodes(scope):
        yield child
        if not isinstance(child, _FUNCTION_SCOPES):
            yield from _own_nodes(child)


def _model_literal(call):
    """The label of an ``apps.get_model`` call written with literal arguments."""
    parts = [arg.value for arg in call.args if isinstance(arg, ast.Constant) and isinstance(arg.value, str)]
    if len(parts) == 1 and "." in parts[0]:
        return parts[0].lower()
    if len(parts) == 2:
        return f"{parts[0]}.{parts[1]}".lower()
    return None


def _label(node, names):
    """The registered label an expression resolves to, or ``None`` when it does not."""
    if isinstance(node, ast.Name):
        return names.get(node.id)
    if isinstance(node, (ast.Attribute, ast.Subscript)):
        return _label(node.value, names)
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Attribute):
            if node.func.attr == "get_model":
                literal = _model_literal(node)
                if literal is not None:
                    return literal
            if node.func.attr in _COPY_HELPERS and node.args:
                return _label(node.args[0], names)
        return _label(node.func, names)
    return None


def _bind(names, target, value):
    label = _label(value, names)
    if label is None:
        return
    if isinstance(target, ast.Name):
        names[target.id] = label
    elif isinstance(target, (ast.Tuple, ast.List)) and target.elts:
        head = target.elts[0]
        if isinstance(head, ast.Name):
            names[head.id] = label


def _collect(names, node, registry):
    if isinstance(node, ast.ImportFrom):
        for alias in node.names:
            label = registry.names.get(alias.name)
            if label is not None:
                names[alias.asname or alias.name] = label
    elif isinstance(node, ast.Assign):
        for target in node.targets:
            _bind(names, target, node.value)
    elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
        _bind(names, node.target, node.iter)
    elif isinstance(node, ast.withitem) and node.optional_vars is not None:
        _bind(names, node.optional_vars, node.context_expr)


def _scope_names(scope, inherited, registry):
    names = dict(inherited)
    own = list(_own_nodes(scope))
    for _ in range(_BINDING_PASSES):
        for node in own:
            _collect(names, node, registry)
    return names, own


def _expression(node) -> str:
    """The call target with its arguments elided, so an edited filter keeps its key."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_expression(node.value)}.{node.attr}"
    if isinstance(node, ast.Call):
        return f"{_expression(node.func)}()"
    if isinstance(node, ast.Subscript):
        return f"{_expression(node.value)}[]"
    return "?"


def _sql_text(node, names, registry) -> str:
    """One SQL argument as text, with a resolvable interpolated table name substituted in."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(_sql_text(part, names, registry) for part in node.values)
    if isinstance(node, ast.FormattedValue):
        return registry.db_tables.get(_label(node.value, names), " ")
    return " "


def _qualname(node, parents) -> str:
    parts = []
    current = parents.get(node)
    while current is not None:
        if isinstance(current, (*_FUNCTION_SCOPES, ast.ClassDef)):
            parts.append(current.name)
        current = parents.get(current)
    return ".".join(reversed(parts)) or "<module>"


def _parents(tree) -> dict:
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _call_sites(node, names, registry, module, parents, found):
    """Record what one call mutates, whether through the ORM or through raw DML."""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return
    labels = []
    if node.func.attr in _MUTATION_METHODS:
        labels.append(_label(node.func.value, names))
        expression = _expression(node.func)
    elif node.func.attr == "execute":
        for argument in node.args:
            text = _sql_text(argument, names, registry)
            labels.extend(registry.tables.get(table.lower()) for table in _DML_TARGET.findall(text))
        expression = f"{_expression(node.func)}()"
    else:
        return
    for label in labels:
        if label in registry.labels:
            found.append(_Site(module, _qualname(node, parents), expression, label, node.lineno))


def _scan(scope, inherited, registry, module, parents, found):
    names, own = _scope_names(scope, inherited, registry)
    for node in own:
        _call_sites(node, names, registry, module, parents, found)
    for node in own:
        if isinstance(node, _FUNCTION_SCOPES):
            _scan(node, names, registry, module, parents, found)


def _module_sites(path, module, registry) -> list:
    tree = ast.parse(path.read_text(), filename=str(path))
    found: list[_Site] = []
    _scan(tree, {}, registry, module, _parents(tree), found)
    return found


def _production_modules():
    package = Path(__file__).resolve().parents[1]
    for path in sorted(package.rglob("*.py")):
        relative = path.relative_to(package).as_posix()
        if relative.startswith(("migrations/", "tests/")):
            continue
        yield path, relative


class TestRendererWriterStructure(SimpleTestCase):
    def _sites(self):
        registry = _registry()
        found = []
        for path, relative in _production_modules():
            found.extend(_module_sites(path, relative, registry))
        return found

    def test_every_registered_model_mutation_call_site_is_a_reviewed_writer_seam(self):
        offenders = sorted(
            (site.module, site.function, site.expression, site.label, site.lineno)
            for site in self._sites()
            if site.key not in _REVIEWED_MUTATION_SITES
        )

        self.assertEqual(offenders, [])

    def test_the_reviewed_call_site_list_carries_no_entry_the_tree_lost(self):
        """A site that moved or went away must leave the list, or the next one inherits it."""
        live = {site.key for site in self._sites()}

        self.assertEqual(sorted(_REVIEWED_MUTATION_SITES - live), [])

    def test_no_process_global_sql_or_implicit_permit_guard_remains(self):
        forbidden = {
            "_IMPLICIT_PERMITS",
            "_authorize_dml",
            "_begin_delete_implicit",
            "_begin_implicit",
            "_begin_m2m_implicit",
            "_discard_rolled_back_implicit_permit",
            "_dml_guard",
            "_end_implicit",
            "_end_m2m_implicit",
            "_install_guard",
            "_parse_dml_target",
        }
        found = set()
        for path, _relative in _production_modules():
            tree = ast.parse(path.read_text(), filename=str(path))
            found.update(node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and node.id in forbidden)

        self.assertEqual(found, set())

    def test_signals_do_not_keep_retired_mutation_paths(self):
        path = Path(__file__).resolve().parents[1] / "signals.py"
        tree = ast.parse(path.read_text(), filename=str(path))
        functions = {node.name for node in ast.walk(tree) if isinstance(node, _FUNCTION_SCOPES)}
        retired = {
            "_create_greenfield_subif_state",
            "_on_routing_static_route_pre_save",
            "_remove_static_route_for_device",
            "_static_route_content",
            "_transition_static_route_content",
        }
        local_copy_imports = [
            node.lineno
            for function in (node for node in ast.walk(tree) if isinstance(node, _FUNCTION_SCOPES))
            for node in ast.walk(function)
            if isinstance(node, ast.Import) and any(alias.name == "copy" for alias in node.names)
        ]

        self.assertEqual(sorted(functions & retired), [])
        self.assertEqual(local_copy_imports, [])

    def test_mtu_inline_edits_delegate_to_an_exact_plan(self):
        path = Path(__file__).resolve().parents[1] / "views.py"
        tree = ast.parse(path.read_text(), filename=str(path))
        target = next(
            node for node in tree.body if isinstance(node, _FUNCTION_SCOPES) and node.name == "_save_owned_overlay_edit"
        )
        calls = {_dotted(node.func) for node in ast.walk(target) if isinstance(node, ast.Call)}

        self.assertIn("_save_owned_interface_mtu_edit", calls)
        self.assertNotIn("obj.save", calls)
        self.assertNotIn("iface.save", calls)


#: The seams that acquire the locks a caller-owned plan is then consumed under. Entering one
#: re-pends the scope's deploying rows (``intent_state._repend_locked_rows``).
_LOCK_CONTEXTS = frozenset({"_intent_transaction", "intent_transaction", "mirror_transaction"})
#: The seed builder every frozen plan comes from, as written at its call sites.
_PLAN_BUILDER = "RendererMutationPlan.build"
#: A helper may front the seed (``_demotion_plan``) and a local name may alias another
#: (``plan = plans[scope]``), so both derivations are re-read until they settle.
_BUILDER_PASSES = 3
_NESTED_SCOPES = (*_FUNCTION_SCOPES, ast.ClassDef, ast.Lambda)


def _owned_nodes(node):
    """Yield a node's lexical subtree without entering nested function scopes."""
    if isinstance(node, _NESTED_SCOPES):
        return
    yield node
    for child in ast.iter_child_nodes(node):
        yield from _owned_nodes(child)


def _owned_body_nodes(body):
    """Yield nodes owned by one statement body."""
    for statement in body:
        yield from _owned_nodes(statement)


def _dotted(node) -> str:
    """A call target as dotted source text, so ``RendererMutationPlan.build`` is one key."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    return "?"


def _builds_a_plan(node, builders) -> bool:
    """Whether *node*'s subtree calls anything that hands back a freshly frozen plan."""
    return any(isinstance(child, ast.Call) and _dotted(child.func) in builders for child in _owned_nodes(node))


def _root_name(node):
    """The local name an expression reads, through any chain of indexes and attributes."""
    while isinstance(node, (ast.Subscript, ast.Attribute)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _bindings(node):
    """Every ``(targets, value)`` pair *node*'s subtree binds, in the three binding forms."""
    for child in _owned_nodes(node):
        if isinstance(child, ast.Assign):
            yield child.targets, child.value
        elif isinstance(child, (ast.For, ast.AsyncFor, ast.comprehension)):
            yield [child.target], child.iter
        elif isinstance(child, ast.withitem) and child.optional_vars is not None:
            yield [child.optional_vars], child.context_expr


def _plan_names(nodes, builders) -> set:
    """Every local name *nodes* bind to a plan built there, aliases included."""
    bound: set[str] = set()
    for _ in range(_BUILDER_PASSES):
        for node in nodes:
            for targets, value in _bindings(node):
                if not _builds_a_plan(value, builders) and _root_name(value) not in bound:
                    continue
                for target in targets:
                    elements = target.elts if isinstance(target, (ast.Tuple, ast.List)) else [target]
                    bound.update(element.id for element in elements if isinstance(element, ast.Name))
    return bound


def _direct_bindings(node):
    """Bindings made by one statement, excluding its nested statement bodies."""
    if isinstance(node, ast.Assign):
        yield node.targets, node.value
    elif isinstance(node, ast.AnnAssign) and node.value is not None:
        yield [node.target], node.value
    elif isinstance(node, (ast.For, ast.AsyncFor)):
        yield [node.target], node.iter
    elif isinstance(node, (ast.With, ast.AsyncWith)):
        for item in node.items:
            if item.optional_vars is not None:
                yield [item.optional_vars], item.context_expr


def _direct_plan_names(nodes, builders) -> set:
    """Plan names bound directly by these statements, aliases included."""
    bound: set[str] = set()
    for _ in range(_BUILDER_PASSES):
        for node in nodes:
            for targets, value in _direct_bindings(node):
                if not _builds_a_plan(value, builders) and _root_name(value) not in bound:
                    continue
                for target in targets:
                    elements = target.elts if isinstance(target, (ast.Tuple, ast.List)) else [target]
                    bound.update(element.id for element in elements if isinstance(element, ast.Name))
    return bound


def _statement_bodies(statement):
    """Yield nested statement lists owned by one compound statement."""
    for name in ("body", "orelse", "finalbody"):
        body = getattr(statement, name, None)
        if body:
            yield body
    for handler in getattr(statement, "handlers", ()):
        if handler.body:
            yield handler.body
    for case in getattr(statement, "cases", ()):
        if case.body:
            yield case.body


def _contains_node(statement, target) -> bool:
    return any(node is target for node in _owned_nodes(statement))


def _statements_before(body, target):
    """Return direct bindings that dominate target along its enclosing statement path."""
    preceding = []
    for index, statement in enumerate(body):
        if not _contains_node(statement, target):
            continue
        preceding.extend(body[:index])
        preceding.append(statement)
        for nested in _statement_bodies(statement):
            if any(_contains_node(child, target) for child in nested):
                preceding.extend(_statements_before(nested, target))
                break
        return preceding
    return preceding


def _plan_builders(tree) -> set:
    """``RendererMutationPlan.build`` plus every module-local helper that returns its result."""
    builders = {_PLAN_BUILDER}
    functions = [node for node in ast.walk(tree) if isinstance(node, _FUNCTION_SCOPES)]
    for _ in range(_BUILDER_PASSES):
        for function in functions:
            names = _plan_names(function.body, builders)
            returned = [
                node.value for node in _owned_body_nodes(function.body) if isinstance(node, ast.Return) and node.value
            ]
            hands_one_back = any(
                _builds_a_plan(value, builders)
                or any(isinstance(part, ast.Name) and part.id in names for part in ast.walk(value))
                for value in returned
            )
            if hands_one_back:
                builders.add(function.name)
    return builders


def _lock_contexts(tree):
    """Each ``with intent_transaction(...)`` / ``mirror_transaction(...)`` statement."""
    for node in ast.walk(tree):
        if isinstance(node, ast.With) and any(
            isinstance(item.context_expr, ast.Call) and _dotted(item.context_expr.func) in _LOCK_CONTEXTS
            for item in node.items
        ):
            yield node


def _consumers(tree):
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _dotted(node.func) == "consume_renderer_plan" and node.args
    ]


def _stale_plan_source(source, module) -> list:
    """Every ``consume_renderer_plan`` whose plan was frozen before its own locks.

    Entering the transaction re-pends the scope's deploying rows, and
    ``RendererWriter._find_save`` compares the FULL pre-image, so a plan frozen before the
    ``with`` loses to the very transaction that consumes it. A pre-transaction pass that only
    derives the lock footprint stays legal and is not reported: the rule reads the name the
    consumer takes, which a second in-transaction build has to rebind.

    ``renderer_writes`` is deliberately out of scope. It opens its own transaction with
    ``repend_after=True``, so the repend lands after the body and cannot invalidate a plan
    built before the call. Only a CALLER-owned lock context has that hazard.
    """
    tree = ast.parse(source, filename=module)
    builders = _plan_builders(tree)
    pending = _consumers(tree)
    offenders = []
    for statement in _lock_contexts(tree):
        inside = set(_owned_body_nodes(statement.body))
        for call in [node for node in pending if node in inside]:
            pending.remove(call)
            plan = call.args[0]
            bound = _direct_plan_names(_statements_before(statement.body, call), builders)
            if not _builds_a_plan(plan, builders) and _root_name(plan) not in bound:
                offenders.append((module, ast.unparse(plan), call.lineno))
    # A consumer that no lock context encloses at all has no locks to be planned under.
    offenders.extend((module, ast.unparse(call.args[0]), call.lineno) for call in pending)
    return offenders


def _stale_plan_sites(path, module) -> list:
    return _stale_plan_source(path.read_text(), module)


class TestPlansAreBuiltUnderTheLocksThatConsumeThem(SimpleTestCase):
    def test_no_consumed_plan_is_frozen_before_its_own_lock_transaction(self):
        offenders = []
        for path, relative in _production_modules():
            offenders.extend(_stale_plan_sites(path, relative))

        self.assertEqual(sorted(offenders), [])

    def test_a_later_rebuild_does_not_authorize_an_earlier_stale_plan(self):
        source = """
def repair():
    plan = RendererMutationPlan.build()
    with intent_transaction(footprint):
        with consume_renderer_plan(plan, permit):
            repair_row()
        plan = RendererMutationPlan.build()
"""

        self.assertEqual(
            _stale_plan_source(source, "fixture.py"),
            [("fixture.py", "plan", 5)],
        )

    def test_a_nested_plan_return_does_not_make_its_outer_function_a_builder(self):
        source = """
def outer():
    def nested():
        return RendererMutationPlan.build()
    return None

def repair():
    with intent_transaction(footprint):
        plan = outer()
        consume_renderer_plan(plan, permit)
"""

        self.assertEqual(
            _stale_plan_source(source, "fixture.py"),
            [("fixture.py", "plan", 10)],
        )

    def test_a_deferred_nested_consumer_is_not_inside_its_defining_lock(self):
        source = """
def repair():
    plan = RendererMutationPlan.build()
    with intent_transaction(footprint):
        def later():
            consume_renderer_plan(plan, permit)
    return later
"""

        self.assertEqual(
            _stale_plan_source(source, "fixture.py"),
            [("fixture.py", "plan", 6)],
        )

    def test_the_guard_still_reaches_the_call_sites_it_polices(self):
        """A rule that resolves nothing passes for free, so pin what it actually reads."""
        modules = set()
        builders = {}
        for path, relative in _production_modules():
            tree = ast.parse(path.read_text(), filename=str(path))
            if _consumers(tree):
                modules.add(relative)
                builders[relative] = sorted(_plan_builders(tree) - {_PLAN_BUILDER})

        self.assertEqual(sorted(modules), ["ownership_planner.py", "renderer_audit.py"])
        self.assertIn("_demotion_plan", builders["ownership_planner.py"])
        self.assertIn("_repair_plan", builders["renderer_audit.py"])
