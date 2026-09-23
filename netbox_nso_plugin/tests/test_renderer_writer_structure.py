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
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from netbox_nso_plugin.tests._ast_scope import scoped_walk

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
        ("onboarding.py", "_submit_claimed_provision", "NSOPlatformNedMapping.objects.get_or_create"),
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
    elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)):
        _bind(names, node.target, node.value)
    elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
        _bind(names, node.target, node.iter)
    elif isinstance(node, ast.withitem) and node.optional_vars is not None:
        _bind(names, node.optional_vars, node.context_expr)


def _scope_names(scope, inherited, registry):
    names = dict(inherited)
    own = list(_own_nodes(scope))
    previous = None
    seen = set()
    while names != previous:
        bindings = tuple(sorted(names.items()))
        if bindings in seen:
            raise ValueError("model bindings do not converge")
        seen.add(bindings)
        previous = dict(names)
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
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
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


def _mtu_delegation_offenders(source) -> list[str]:
    tree = ast.parse(source)
    target = next(
        node for node in tree.body if isinstance(node, _FUNCTION_SCOPES) and node.name == "_save_owned_overlay_edit"
    )
    calls = {_dotted(node.func) for node in scoped_walk(target.body) if isinstance(node, ast.Call)}
    whole_tree_calls = {_dotted(node.func) for node in ast.walk(target) if isinstance(node, ast.Call)}
    if "_save_owned_interface_mtu_edit" not in calls or whole_tree_calls & {"obj.save", "iface.save"}:
        return [target.name]
    return []


class TestRendererBindingCollector(SimpleTestCase):
    def test_assignment_forms_preserve_model_bindings(self):
        sources = (
            "row: VLAN = VLAN.objects.first()\nrow.save()",
            "if (row := VLAN.objects.first()):\n    row.save()",
            "a = b\nb = c\nc = d\nd = e\ne = VLAN\na.objects.update(name='changed')",
        )
        registry = _registry()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bindings.py"
            for source in sources:
                with self.subTest(source=source):
                    path.write_text("from ipam.models import VLAN\n" + source + "\n", encoding="utf-8")
                    sites = _module_sites(path, path.name, registry)
                    self.assertEqual([site.label for site in sites], ["ipam.vlan"])

    def test_creation_tuple_does_not_bind_the_boolean_as_a_model(self):
        registry = _registry()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bindings.py"
            for method in ("get_or_create", "update_or_create"):
                with self.subTest(method=method):
                    path.write_text(
                        "from ipam.models import VLAN\n"
                        f"row, created = VLAN.objects.{method}(vid=100)\n"
                        "row.save()\ncreated.save()\n",
                        encoding="utf-8",
                    )
                    sites = _module_sites(path, path.name, registry)
                    self.assertEqual([site.expression for site in sites], [f"VLAN.objects.{method}", "row.save"])

    def test_conflicting_cyclic_aliases_fail_explicitly(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bindings.py"
            path.write_text(
                "from ipam.models import VLAN, IPAddress\n"
                "from dcim.models import Interface\n"
                "a = VLAN\nb = Interface\nc = IPAddress\n"
                "def mutate():\n"
                "    global a, b, c\n"
                "    a = b\n    b = c\n    c = a\n    a.objects.update()\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "model bindings do not converge"):
                _module_sites(path, path.name, _registry())


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

    def test_signals_do_not_import_copy_inside_a_function(self):
        path = Path(__file__).resolve().parents[1] / "signals.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        local_copy_imports = [
            node.lineno
            for function in (node for node in ast.walk(tree) if isinstance(node, _FUNCTION_SCOPES))
            for node in ast.walk(function)
            if isinstance(node, ast.Import) and any(alias.name == "copy" for alias in node.names)
        ]

        self.assertEqual(local_copy_imports, [])

    def test_mtu_inline_edits_delegate_to_an_exact_plan(self):
        path = Path(__file__).resolve().parents[1] / "views.py"
        self.assertEqual(_mtu_delegation_offenders(path.read_text(encoding="utf-8")), [])

    def test_nested_mtu_delegation_does_not_certify_the_outer_edit(self):
        nested_bodies = {
            "function": "    def nested():\n        _save_owned_interface_mtu_edit()\n",
            "async function": "    async def nested():\n        _save_owned_interface_mtu_edit()\n",
            "lambda": "    nested = lambda: _save_owned_interface_mtu_edit()\n",
            "class method": (
                "    class Nested:\n        def save(self):\n            _save_owned_interface_mtu_edit()\n"
            ),
        }
        for boundary, body in nested_bodies.items():
            with self.subTest(boundary=boundary):
                source = "def _save_owned_overlay_edit():\n" + body
                self.assertEqual(_mtu_delegation_offenders(source), ["_save_owned_overlay_edit"])

        direct_source = "def _save_owned_overlay_edit():\n    _save_owned_interface_mtu_edit()\n"
        self.assertEqual(_mtu_delegation_offenders(direct_source), [])

    def test_nested_forbidden_save_still_fails_the_mtu_delegation_guard(self):
        source = """
def _save_owned_overlay_edit():
    _save_owned_interface_mtu_edit()
    def persist():
        iface.save()
    persist()
"""

        self.assertEqual(_mtu_delegation_offenders(source), ["_save_owned_overlay_edit"])


#: The seams that acquire the locks a caller-owned plan is then consumed under. Entering one
#: re-pends the scope's deploying rows (``intent_state._repend_locked_rows``).
_LOCK_CONTEXTS = frozenset({"_intent_transaction", "intent_transaction", "mirror_transaction"})
#: The seed builder every frozen plan comes from, as written at its call sites.
_PLAN_BUILDER = "RendererMutationPlan.build"
#: A helper may front the seed (``_demotion_plan``) and a local name may alias another
#: (``plan = plans[scope]``), so both derivations are re-read until they settle.


def _dotted(node) -> str:
    """A call target as dotted source text, so ``RendererMutationPlan.build`` is one key."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    return "?"


def _plan_value_nodes(node):
    # Lambdas are opaque because their expression values are callables, not plans.
    if isinstance(node, ast.Lambda):
        return
    yield node
    for child in ast.iter_child_nodes(node):
        yield from _plan_value_nodes(child)


def _builds_a_plan(node, builders) -> bool:
    """Whether *node*'s subtree calls anything that hands back a freshly frozen plan."""
    return any(isinstance(child, ast.Call) and _dotted(child.func) in builders for child in _plan_value_nodes(node))


def _root_name(node):
    """The local name an expression reads, through any chain of indexes and attributes."""
    while isinstance(node, (ast.Subscript, ast.Attribute)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _builder_paths(builders, name) -> frozenset[tuple[int, ...]]:
    if isinstance(builders, dict):
        return builders.get(name, frozenset())
    return frozenset({()}) if name in builders else frozenset()


def _plan_paths(node, bound, builders) -> frozenset[tuple[int, ...]]:
    """Paths through a value that definitely contain a plan on every execution path."""
    if isinstance(node, ast.Lambda):
        return frozenset()
    if isinstance(node, ast.IfExp):
        return _plan_paths(node.body, bound, builders) & _plan_paths(node.orelse, bound, builders)
    if isinstance(node, ast.BoolOp):
        paths = [_plan_paths(value, bound, builders) for value in node.values]
        return frozenset.intersection(*paths) if paths else frozenset()
    if isinstance(node, ast.NamedExpr):
        return _plan_paths(node.value, bound, builders)
    if isinstance(node, ast.Await):
        return _plan_paths(node.value, bound, builders)
    if isinstance(node, ast.Call):
        return _builder_paths(builders, _dotted(node.func))
    if isinstance(node, ast.DictComp):
        return frozenset({()}) if () in _plan_paths(node.value, bound, builders) else frozenset()
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
        return frozenset({()}) if () in _plan_paths(node.elt, bound, builders) else frozenset()
    if isinstance(node, ast.Dict):
        values = [_plan_paths(value, bound, builders) for value in node.values]
        return frozenset({()}) if values and all(() in paths for paths in values) else frozenset()
    if isinstance(node, (ast.List, ast.Set, ast.Tuple)):
        element_paths = [_plan_paths(value, bound, builders) for value in node.elts]
        paths = {(index, *path) for index, value_paths in enumerate(element_paths) for path in value_paths}
        if element_paths and all(() in value_paths for value_paths in element_paths):
            paths.add(())
        return frozenset(paths)
    return frozenset({()}) if _root_name(node) in bound else frozenset()


def _value_holds_plan(node, bound, builders) -> bool:
    return () in _plan_paths(node, bound, builders)


def _bindings(node):
    """Every ``(targets, value)`` pair *node*'s subtree binds, in the three binding forms."""
    for child in scoped_walk(node):
        if isinstance(child, ast.Assign):
            yield child.targets, child.value
        elif isinstance(child, (ast.For, ast.AsyncFor, ast.comprehension)):
            yield [child.target], child.iter
        elif isinstance(child, ast.withitem) and child.optional_vars is not None:
            yield [child.optional_vars], child.context_expr


def _bound_plan_names(nodes, builders, extract) -> set:
    """The alias fixed point over the bindings *extract* reads out of each node."""
    bound: set[str] = set()
    while True:
        previous_size = len(bound)
        for node in nodes:
            for targets, value in extract(node):
                if not _builds_a_plan(value, builders) and _root_name(value) not in bound:
                    continue
                for target in targets:
                    elements = target.elts if isinstance(target, (ast.Tuple, ast.List)) else [target]
                    bound.update(element.id for element in elements if isinstance(element, ast.Name))
        if len(bound) == previous_size:
            return bound


def _plan_names(nodes, builders) -> set:
    """Every local name *nodes* bind to a plan built there, aliases included."""
    return _bound_plan_names(nodes, builders, _bindings)


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


def _target_names(target) -> set[str]:
    """Local names one assignment target writes or deletes."""
    return {
        child.id
        for child in ast.walk(target)
        if isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del))
    }


def _pattern_bound_names(pattern) -> set[str]:
    """Local names one match pattern captures."""
    names = set()
    for child in ast.walk(pattern):
        if isinstance(child, (ast.MatchAs, ast.MatchStar)) and child.name is not None:
            names.add(child.name)
        elif isinstance(child, ast.MatchMapping) and child.rest is not None:
            names.add(child.rest)
    return names


def _target_plan_names(target, paths, prefix=()) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id} if prefix in paths else set()
    if isinstance(target, (ast.Tuple, ast.List)):
        return set().union(
            *(_target_plan_names(element, paths, (*prefix, index)) for index, element in enumerate(target.elts))
        )
    return set()


def _bind_plan_value(bound, targets, value, builders) -> set[str]:
    """Apply one value binding to the definite-plan state."""
    result = set(bound)
    names = set().union(*(_target_names(target) for target in targets))
    result.difference_update(names)
    paths = _plan_paths(value, bound, builders)
    if () in paths:
        result.update(names)
    elif len(targets) == 1 and isinstance(targets[0], (ast.Tuple, ast.List)):
        result.update(_target_plan_names(targets[0], paths))
    return result


def _comprehension_state_after(node, bound, builders) -> set[str]:
    """Apply definite outer-iterable effects and possible iteration effects."""
    outer = _expression_state_after(node.generators[0].iter, bound, builders)
    iteration = set(outer)
    exits = [set(outer)]
    for index, generator in enumerate(node.generators):
        if index:
            iteration = _expression_state_after(generator.iter, iteration, builders)
            exits.append(set(iteration))
        for condition in generator.ifs:
            iteration = _expression_state_after(condition, iteration, builders)
            exits.append(set(iteration))
    if isinstance(node, ast.DictComp):
        iteration = _expression_state_after(node.key, iteration, builders)
        iteration = _expression_state_after(node.value, iteration, builders)
    else:
        iteration = _expression_state_after(node.elt, iteration, builders)
    exits.append(iteration)
    return _merge_plan_states(exits)


def _expression_state_after(node, bound, builders) -> set[str]:
    """Apply assignment expressions in evaluation order, excluding deferred scopes."""
    if isinstance(node, ast.Lambda):
        return set(bound)
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
        return _comprehension_state_after(node, bound, builders)
    if isinstance(node, ast.NamedExpr):
        evaluated = _expression_state_after(node.value, bound, builders)
        return _bind_plan_value(evaluated, [node.target], node.value, builders)
    if isinstance(node, ast.BoolOp):
        result = _expression_state_after(node.values[0], bound, builders)
        for value in node.values[1:]:
            evaluated = _expression_state_after(value, result, builders)
            result = _merge_plan_states((result, evaluated))
        return result
    if isinstance(node, ast.IfExp):
        tested = _expression_state_after(node.test, bound, builders)
        return _merge_plan_states(
            (
                _expression_state_after(node.body, tested, builders),
                _expression_state_after(node.orelse, tested, builders),
            )
        )
    if isinstance(node, ast.Compare):
        result = _expression_state_after(node.left, bound, builders)
        for index, comparator in enumerate(node.comparators):
            evaluated = _expression_state_after(comparator, result, builders)
            result = evaluated if index == 0 else _merge_plan_states((result, evaluated))
        return result
    if isinstance(node, ast.Dict):
        result = set(bound)
        for key, value in zip(node.keys, node.values, strict=True):
            if key is not None:
                result = _expression_state_after(key, result, builders)
            result = _expression_state_after(value, result, builders)
        return result
    result = set(bound)
    for child in ast.iter_child_nodes(node):
        result = _expression_state_after(child, result, builders)
    return result


def _match_case_state(case, bound, builders) -> set[str]:
    """Apply a match case's captures and guard to the definite-plan state."""
    result = set(bound) - _pattern_bound_names(case.pattern)
    if case.guard is not None:
        result = _expression_state_after(case.guard, result, builders)
    return result


def _merge_plan_states(states) -> set[str]:
    states = list(states)
    return set.intersection(*states) if states else set()


def _match_case_states(statement, bound, builders):
    """Return each case entry and the state that can pass every case."""
    remaining = _expression_state_after(statement.subject, bound, builders)
    case_states = []
    for case in statement.cases:
        entered = _match_case_state(case, remaining, builders)
        case_states.append((case, entered))
        if case.guard is not None:
            remaining = _merge_plan_states((remaining, entered))
    return case_states, remaining


@dataclasses.dataclass
class _LoopFlow:
    normal: set[str] | None
    continues: list[set[str]] = dataclasses.field(default_factory=list)
    breaks: list[set[str]] = dataclasses.field(default_factory=list)


def _merge_reachable_states(states) -> set[str] | None:
    reachable = [state for state in states if state is not None]
    return _merge_plan_states(reachable) if reachable else None


def _merge_loop_flows(flows) -> _LoopFlow:
    flows = list(flows)
    return _LoopFlow(
        _merge_reachable_states(flow.normal for flow in flows),
        [state for flow in flows for state in flow.continues],
        [state for flow in flows for state in flow.breaks],
    )


def _flow_through_finally(flow, finalbody, builders) -> _LoopFlow:
    if not finalbody:
        return flow
    output = _LoopFlow(None)

    def apply(state, kind):
        final = _loop_flow_block(finalbody, state, builders)
        if final.normal is not None:
            if kind == "normal":
                output.normal = final.normal
            else:
                getattr(output, kind).append(final.normal)
        output.continues.extend(final.continues)
        output.breaks.extend(final.breaks)

    if flow.normal is not None:
        apply(flow.normal, "normal")
    for state in flow.continues:
        apply(state, "continues")
    for state in flow.breaks:
        apply(state, "breaks")
    return output


def _loop_flow_try(statement, bound, builders) -> _LoopFlow:
    body = _loop_flow_block(statement.body, bound, builders)
    flows = []
    if body.normal is not None:
        flows.append(_loop_flow_block(statement.orelse, body.normal, builders))
    flows.append(_LoopFlow(None, body.continues, body.breaks))
    handler_entry = _handler_entry_state(statement.body, bound, builders)
    for handler in statement.handlers:
        entered = set(handler_entry)
        if handler.name is not None:
            entered.discard(handler.name)
        flows.append(_loop_flow_block(handler.body, entered, builders))
    return _flow_through_finally(_merge_loop_flows(flows), statement.finalbody, builders)


def _with_header_state(statement, bound, builders) -> set[str]:
    entered = set(bound)
    for item in statement.items:
        entered = _expression_state_after(item.context_expr, entered, builders)
        if item.optional_vars is not None:
            entered = _bind_plan_value(entered, [item.optional_vars], item.context_expr, builders)
    return entered


def _loop_flow_statement(statement, bound, builders) -> _LoopFlow:
    if isinstance(statement, ast.Continue):
        return _LoopFlow(None, continues=[set(bound)])
    if isinstance(statement, ast.Break):
        return _LoopFlow(None, breaks=[set(bound)])
    if isinstance(statement, (ast.Return, ast.Raise)):
        return _LoopFlow(None)
    if isinstance(statement, ast.If):
        tested = _expression_state_after(statement.test, bound, builders)
        body = _loop_flow_block(statement.body, tested, builders)
        alternate = _loop_flow_block(statement.orelse, tested, builders)
        return _LoopFlow(
            _merge_reachable_states((body.normal, alternate.normal)),
            body.continues + alternate.continues,
            body.breaks + alternate.breaks,
        )
    if isinstance(statement, (ast.With, ast.AsyncWith)):
        return _loop_flow_block(statement.body, _with_header_state(statement, bound, builders), builders)
    if isinstance(statement, (ast.Try, ast.TryStar)):
        return _loop_flow_try(statement, bound, builders)
    if isinstance(statement, ast.Match):
        case_states, unmatched = _match_case_states(statement, bound, builders)
        flows = [_LoopFlow(unmatched)]
        for case, entered in case_states:
            flows.append(_loop_flow_block(case.body, entered, builders))
        return _merge_loop_flows(flows)
    return _LoopFlow(_plan_state_after(statement, bound, builders))


def _loop_flow_block(body, bound, builders) -> _LoopFlow:
    normal = set(bound)
    continues = []
    breaks = []
    for statement in body:
        if normal is None:
            break
        flow = _loop_flow_statement(statement, normal, builders)
        normal = flow.normal
        continues.extend(flow.continues)
        breaks.extend(flow.breaks)
    return _LoopFlow(normal, continues, breaks)


def _loop_header_state(statement, bound, builders) -> set[str]:
    if isinstance(statement, (ast.For, ast.AsyncFor)):
        evaluated = _expression_state_after(statement.iter, bound, builders)
        return _bind_plan_value(evaluated, [statement.target], statement.iter, builders)
    return _expression_state_after(statement.test, bound, builders)


def _loop_backedge_state(statement, bound, builders) -> set[str]:
    if isinstance(statement, (ast.For, ast.AsyncFor)):
        return _bind_plan_value(bound, [statement.target], statement.iter, builders)
    return _loop_header_state(statement, bound, builders)


def _loop_body_entry(statement, bound, builders) -> set[str]:
    """Definite plan names at the body entry across the first and later iterations."""
    first = _loop_header_state(statement, bound, builders)
    entry = set(first)
    while True:
        previous = set(entry)
        flow = _loop_flow_block(statement.body, entry, builders)
        backedges = [*flow.continues]
        if flow.normal is not None:
            backedges.append(flow.normal)
        backedges = [_loop_backedge_state(statement, state, builders) for state in backedges]
        if backedges:
            entry.intersection_update(_merge_plan_states(backedges))
        if entry == previous:
            return entry


def _loop_no_break_state(statement, bound, builders) -> set[str]:
    entered = _loop_body_entry(statement, bound, builders)
    flow = _loop_flow_block(statement.body, entered, builders)
    zero_iterations = (
        _expression_state_after(statement.iter, bound, builders)
        if isinstance(statement, (ast.For, ast.AsyncFor))
        else entered
    )
    exits = [zero_iterations, *flow.continues]
    if flow.normal is not None:
        exits.append(flow.normal)
    return _merge_plan_states(exits)


def _loop_state_after(statement, bound, builders) -> set[str]:
    entered = _loop_body_entry(statement, bound, builders)
    flow = _loop_flow_block(statement.body, entered, builders)
    no_break = _loop_no_break_state(statement, bound, builders)
    exits = [_plan_state_after_block(statement.orelse, no_break, builders), *flow.breaks]
    return _merge_plan_states(exits)


def _handler_entry_state(body, bound, builders) -> set[str]:
    """Plans that survive every statement prefix from which a handler can run."""
    entry = set(bound)
    current = set(bound)
    for statement in body:
        for inner_entry in _compound_handler_entry_states(statement, current, builders):
            entry.intersection_update(inner_entry)
        current = _plan_state_after(statement, current, builders)
        entry.intersection_update(current)
    return entry


def _compound_handler_entry_states(statement, bound, builders) -> list[set[str]]:
    if isinstance(statement, ast.If):
        tested = _expression_state_after(statement.test, bound, builders)
        return [
            _handler_entry_state(statement.body, tested, builders),
            _handler_entry_state(statement.orelse, tested, builders),
        ]
    if isinstance(statement, (ast.With, ast.AsyncWith)):
        entered = _with_header_state(statement, bound, builders)
        return [_handler_entry_state(statement.body, entered, builders)]
    if isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
        entered = _loop_body_entry(statement, bound, builders)
        return [
            _handler_entry_state(statement.body, entered, builders),
            _handler_entry_state(statement.orelse, _loop_no_break_state(statement, bound, builders), builders),
        ]
    if isinstance(statement, (ast.Try, ast.TryStar)):
        body_entry = _handler_entry_state(statement.body, bound, builders)
        normal = _plan_state_after_block(statement.body, bound, builders)
        orelse_entry = _handler_entry_state(statement.orelse, normal, builders)
        handler_states = []
        handler_entries = []
        for handler in statement.handlers:
            handler_bound = set(body_entry)
            if handler.name is not None:
                handler_bound.discard(handler.name)
            handler_states.append(_plan_state_after_block(handler.body, handler_bound, builders))
            handler_entries.append(_handler_entry_state(handler.body, handler_bound, builders))
        final_bound = _merge_plan_states(
            [
                _plan_state_after_block(statement.orelse, normal, builders),
                *handler_states,
                body_entry,
                orelse_entry,
                *handler_entries,
            ]
        )
        final_entry = _handler_entry_state(statement.finalbody, final_bound, builders)
        return [body_entry, orelse_entry, *handler_entries, final_entry]
    if isinstance(statement, ast.Match):
        case_states, _unmatched = _match_case_states(statement, bound, builders)
        return [_handler_entry_state(case.body, entered, builders) for case, entered in case_states]
    return []


def _plan_state_after_compound(statement, bound, builders) -> set[str]:
    if isinstance(statement, ast.If):
        tested = _expression_state_after(statement.test, bound, builders)
        return _merge_plan_states(
            (
                _plan_state_after_block(statement.body, tested, builders),
                _plan_state_after_block(statement.orelse, tested, builders),
            )
        )
    if isinstance(statement, (ast.With, ast.AsyncWith)):
        return _plan_state_after_block(statement.body, _with_header_state(statement, bound, builders), builders)
    if isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
        return _loop_state_after(statement, bound, builders)
    if isinstance(statement, (ast.Try, ast.TryStar)):
        normal = _plan_state_after_block(statement.body, bound, builders)
        normal = _plan_state_after_block(statement.orelse, normal, builders)
        branches = [normal]
        handler_entry = _handler_entry_state(statement.body, bound, builders)
        for handler in statement.handlers:
            handler_bound = set(handler_entry)
            if handler.name is not None:
                handler_bound.discard(handler.name)
            branches.append(_plan_state_after_block(handler.body, handler_bound, builders))
        return _plan_state_after_block(statement.finalbody, _merge_plan_states(branches), builders)
    if isinstance(statement, ast.Match):
        case_states, unmatched = _match_case_states(statement, bound, builders)
        branches = [unmatched]
        branches.extend(_plan_state_after_block(case.body, entered, builders) for case, entered in case_states)
        return _merge_plan_states(branches)
    return set(bound)


def _plan_state_after(statement, bound, builders) -> set[str]:
    """Names that definitely hold a plan after one statement completes normally."""
    if isinstance(statement, ast.Assign):
        evaluated = _expression_state_after(statement.value, bound, builders)
        return _bind_plan_value(evaluated, statement.targets, statement.value, builders)
    if isinstance(statement, ast.AnnAssign) and statement.value is not None:
        evaluated = _expression_state_after(statement.value, bound, builders)
        return _bind_plan_value(evaluated, [statement.target], statement.value, builders)
    if isinstance(statement, ast.AugAssign):
        return set(bound) - _target_names(statement.target)
    if isinstance(statement, ast.Delete):
        deleted = set().union(*(_target_names(target) for target in statement.targets))
        return set(bound) - deleted
    if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return set(bound) - {statement.name}
    if isinstance(statement, (ast.Import, ast.ImportFrom)):
        imported = {alias.asname or alias.name.split(".")[0] for alias in statement.names}
        return set(bound) - imported
    if isinstance(statement, ast.Expr):
        return _expression_state_after(statement.value, bound, builders)
    return _plan_state_after_compound(statement, bound, builders)


def _plan_state_after_block(nodes, bound, builders) -> set[str]:
    result = set(bound)
    for node in nodes:
        result = _plan_state_after(node, result, builders)
    return result


def _direct_plan_names(nodes, builders) -> set:
    """Names that definitely hold a plan after the statements run in source order."""
    return _plan_state_after_block(nodes, set(), builders)


def _contains_node(statement, target) -> bool:
    return any(node is target for node in scoped_walk(statement))


def _body_contains(body, target) -> bool:
    return any(_contains_node(node, target) for node in body)


def _plan_names_before_loop(statement, target, builders, bound) -> set[str]:
    entered = _loop_body_entry(statement, bound, builders)
    if _body_contains(statement.body, target):
        return _plan_names_before(statement.body, target, builders, entered)
    return _plan_names_before(
        statement.orelse,
        target,
        builders,
        _loop_no_break_state(statement, bound, builders),
    )


def _plan_names_before_try(statement, target, builders, bound) -> set[str]:
    if _body_contains(statement.body, target):
        return _plan_names_before(statement.body, target, builders, bound)
    normal = _plan_state_after_block(statement.body, bound, builders)
    if _body_contains(statement.orelse, target):
        return _plan_names_before(statement.orelse, target, builders, normal)
    handler_states = []
    handler_entry = _handler_entry_state(statement.body, bound, builders)
    exceptional_states = [handler_entry, _handler_entry_state(statement.orelse, normal, builders)]
    for handler in statement.handlers:
        handler_bound = set(handler_entry)
        if handler.name is not None:
            handler_bound.discard(handler.name)
        if _body_contains(handler.body, target):
            return _plan_names_before(handler.body, target, builders, handler_bound)
        handler_states.append(_plan_state_after_block(handler.body, handler_bound, builders))
        exceptional_states.append(_handler_entry_state(handler.body, handler_bound, builders))
    branches = [_plan_state_after_block(statement.orelse, normal, builders), *handler_states, *exceptional_states]
    return _plan_names_before(statement.finalbody, target, builders, _merge_plan_states(branches))


def _plan_names_inside(statement, target, builders, bound) -> set[str]:
    if isinstance(statement, ast.If):
        branch = statement.body if _body_contains(statement.body, target) else statement.orelse
        tested = _expression_state_after(statement.test, bound, builders)
        return _plan_names_before(branch, target, builders, tested)
    if isinstance(statement, (ast.With, ast.AsyncWith)):
        return _plan_names_before(statement.body, target, builders, _with_header_state(statement, bound, builders))
    if isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
        return _plan_names_before_loop(statement, target, builders, bound)
    if isinstance(statement, (ast.Try, ast.TryStar)):
        return _plan_names_before_try(statement, target, builders, bound)
    if isinstance(statement, ast.Match):
        case_states, _unmatched = _match_case_states(statement, bound, builders)
        for case, entered in case_states:
            if _body_contains(case.body, target):
                return _plan_names_before(case.body, target, builders, entered)
    return set(bound)


def _plan_names_before(body, target, builders, bound=None) -> set[str]:
    """Names that definitely hold a plan on every path that reaches *target*."""
    result = set() if bound is None else set(bound)
    for statement in body:
        if _contains_node(statement, target):
            return _plan_names_inside(statement, target, builders, result)
        result = _plan_state_after(statement, result, builders)
    return result


def _statement_may_fall_through(statement) -> bool:
    if isinstance(statement, (ast.Return, ast.Raise)):
        return False
    if isinstance(statement, ast.If):
        return _block_may_fall_through(statement.body) or _block_may_fall_through(statement.orelse)
    if isinstance(statement, (ast.With, ast.AsyncWith)):
        return _block_may_fall_through(statement.body)
    if isinstance(statement, (ast.Try, ast.TryStar)):
        if statement.finalbody and not _block_may_fall_through(statement.finalbody):
            return False
        normal = _block_may_fall_through(statement.body) and _block_may_fall_through(statement.orelse)
        return normal or any(_block_may_fall_through(handler.body) for handler in statement.handlers)
    return True


def _block_may_fall_through(body) -> bool:
    return all(_statement_may_fall_through(statement) for statement in body)


def _plan_builders(tree) -> dict[str, frozenset[tuple[int, ...]]]:
    """``RendererMutationPlan.build`` plus every module-local helper that returns its result."""
    builders = {_PLAN_BUILDER: frozenset({()})}
    functions = [node for node in ast.walk(tree) if isinstance(node, _FUNCTION_SCOPES)]
    while True:
        previous = dict(builders)
        for function in functions:
            returned = [node for node in scoped_walk(function.body) if isinstance(node, ast.Return)]
            if not returned or _block_may_fall_through(function.body):
                continue
            returned_paths = [
                (
                    _plan_paths(
                        statement.value,
                        _plan_names_before(function.body, statement, builders),
                        builders,
                    )
                    if statement.value is not None
                    else frozenset()
                )
                for statement in returned
            ]
            common_paths = frozenset.intersection(*returned_paths)
            if common_paths:
                builders[function.name] = common_paths
        if builders == previous:
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
    locks = sorted(_lock_contexts(tree), key=lambda statement: sum(1 for _ in scoped_walk(statement.body)))
    for statement in locks:
        inside = set(scoped_walk(statement.body))
        for call in [node for node in pending if node in inside]:
            pending.remove(call)
            plan = call.args[0]
            bound = _plan_names_before(statement.body, call, builders)
            if not _value_holds_plan(plan, bound, builders):
                offenders.append((module, ast.unparse(plan), call.lineno))
    # A consumer that no lock context encloses at all has no locks to be planned under.
    offenders.extend((module, ast.unparse(call.args[0]), call.lineno) for call in pending)
    return offenders


def _stale_plan_sites(path, module) -> list:
    return _stale_plan_source(path.read_text(encoding="utf-8"), module)


class TestPlansAreBuiltUnderTheLocksThatConsumeThem(SimpleTestCase):
    def test_no_consumed_plan_is_frozen_before_its_own_lock_transaction(self):
        offenders = []
        for path, relative in _production_modules():
            offenders.extend(_stale_plan_sites(path, relative))

        self.assertEqual(sorted(offenders), [])

    def test_both_plan_name_collectors_share_one_alias_fixed_point(self):
        source = """
def repair():
    third = second
    second = first
    first = RendererMutationPlan.build()
"""
        body = ast.parse(source).body[0].body

        self.assertEqual(_plan_names(body, {_PLAN_BUILDER}), {"first", "second", "third"})
        self.assertEqual(_direct_plan_names(body, {_PLAN_BUILDER}), {"first"})

    def test_reverse_ordered_plan_aliases_reach_their_fixed_point(self):
        source = """
def repair():
    fourth = third
    third = second
    second = first
    first = RendererMutationPlan.build()
"""
        body = ast.parse(source).body[0].body

        expected = {"first", "second", "third", "fourth"}
        self.assertEqual(_plan_names(body, {_PLAN_BUILDER}), expected)
        self.assertEqual(_direct_plan_names(body, {_PLAN_BUILDER}), {"first"})

    def test_reverse_ordered_plan_builders_reach_their_fixed_point(self):
        source = """
def fourth():
    return third()
def third():
    return second()
def second():
    return first()
def first():
    return RendererMutationPlan.build()
def repair():
    with intent_transaction(footprint):
        plan = fourth()
        consume_renderer_plan(plan, permit)
"""

        self.assertEqual(_stale_plan_source(source, "fixture.py"), [])

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

    def test_a_later_non_plan_binding_invalidates_an_in_lock_plan(self):
        source = """
def repair(stale):
    with intent_transaction(footprint):
        plan = RendererMutationPlan.build()
        plan = stale
        consume_renderer_plan(plan, permit)
"""

        self.assertEqual(
            _stale_plan_source(source, "fixture.py"),
            [("fixture.py", "plan", 6)],
        )

    def test_a_named_expression_invalidates_an_in_lock_plan(self):
        source = """
def repair(stale):
    with intent_transaction(footprint):
        plan = RendererMutationPlan.build()
        (plan := stale)
        consume_renderer_plan(plan, permit)
"""

        self.assertEqual(
            _stale_plan_source(source, "fixture.py"),
            [("fixture.py", "plan", 6)],
        )

    def test_a_nested_named_expression_invalidates_an_in_lock_plan(self):
        source = """
def repair(stale):
    with intent_transaction(footprint):
        plan = RendererMutationPlan.build()
        ignored = (plan := stale)
        consume_renderer_plan(plan, permit)
"""

        self.assertEqual(
            _stale_plan_source(source, "fixture.py"),
            [("fixture.py", "plan", 6)],
        )

    def test_loop_and_with_headers_invalidate_an_in_lock_plan(self):
        for header in (
            "for item in (plan := [stale])",
            "while (plan := stale)",
            "with nullcontext(plan := stale)",
        ):
            with self.subTest(header=header):
                source = f"""
def repair(stale):
    with intent_transaction(footprint):
        plan = RendererMutationPlan.build()
        {header}:
            consume_renderer_plan(plan, permit)
"""
                self.assertEqual(
                    _stale_plan_source(source, "fixture.py"),
                    [("fixture.py", "plan", 6)],
                )

    def test_a_with_header_invalidates_a_plan_used_after_the_body(self):
        source = """
def repair(stale):
    with intent_transaction(footprint):
        plan = RendererMutationPlan.build()
        with nullcontext(plan := stale):
            pass
        consume_renderer_plan(plan, permit)
"""

        self.assertEqual(_stale_plan_source(source, "fixture.py"), [("fixture.py", "plan", 7)])

    def test_a_with_header_in_a_loop_invalidates_a_later_plan_use(self):
        source = """
def repair(stale):
    with intent_transaction(footprint):
        plan = RendererMutationPlan.build()
        for item in items:
            with nullcontext(plan := stale):
                pass
            consume_renderer_plan(plan, permit)
"""

        self.assertEqual(_stale_plan_source(source, "fixture.py"), [("fixture.py", "plan", 8)])

    def test_a_loop_header_can_build_the_plan_used_in_its_body(self):
        source = """
def repair():
    with intent_transaction(footprint):
        while (plan := RendererMutationPlan.build()):
            consume_renderer_plan(plan, permit)
            break
"""

        self.assertEqual(_stale_plan_source(source, "fixture.py"), [])

    def test_a_for_iterable_does_not_rebuild_the_plan_on_each_iteration(self):
        source = """
def repair(stale):
    with intent_transaction(footprint):
        for item in (plan := RendererMutationPlan.build(), 1):
            consume_renderer_plan(plan, permit)
            plan = stale
"""

        self.assertEqual(_stale_plan_source(source, "fixture.py"), [("fixture.py", "plan", 5)])

    def test_an_empty_for_iterable_does_not_bind_its_target(self):
        source = """
def repair(stale, items):
    with intent_transaction(footprint):
        plan = stale
        for plan in [RendererMutationPlan.build() for _ in items]:
            pass
        consume_renderer_plan(plan, permit)
"""

        self.assertEqual(_stale_plan_source(source, "fixture.py"), [("fixture.py", "plan", 7)])

    def test_a_short_circuited_plan_rebuild_does_not_certify_the_plan(self):
        source = """
def repair(stale, skip):
    with intent_transaction(footprint):
        plan = stale
        skip or (plan := RendererMutationPlan.build())
        consume_renderer_plan(plan, permit)
"""

        self.assertEqual(
            _stale_plan_source(source, "fixture.py"),
            [("fixture.py", "plan", 6)],
        )

    def test_a_conditional_expression_rebuild_does_not_certify_the_plan(self):
        source = """
def repair(stale, rebuild):
    with intent_transaction(footprint):
        plan = stale
        (plan := RendererMutationPlan.build()) if rebuild else None
        consume_renderer_plan(plan, permit)
"""

        self.assertEqual(
            _stale_plan_source(source, "fixture.py"),
            [("fixture.py", "plan", 6)],
        )

    def test_dictionary_bindings_follow_runtime_pair_order(self):
        source = """
def repair(stale):
    with intent_transaction(footprint):
        plan = stale
        {0: (plan := RendererMutationPlan.build()), (plan := stale): 1}
        consume_renderer_plan(plan, permit)
"""

        self.assertEqual(
            _stale_plan_source(source, "fixture.py"),
            [("fixture.py", "plan", 6)],
        )

    def test_a_comprehension_filter_can_skip_a_later_plan_rebuild(self):
        source = """
def repair(stale):
    with intent_transaction(footprint):
        plan = RendererMutationPlan.build()
        [(plan := RendererMutationPlan.build()) for _ in items if (plan := stale) if False]
        consume_renderer_plan(plan, permit)
"""

        self.assertEqual(
            _stale_plan_source(source, "fixture.py"),
            [("fixture.py", "plan", 6)],
        )

    def test_a_conditional_non_plan_binding_invalidates_an_in_lock_plan(self):
        source = """
def repair(stale, replace):
    with intent_transaction(footprint):
        plan = RendererMutationPlan.build()
        if replace:
            plan = stale
        consume_renderer_plan(plan, permit)
"""

        self.assertEqual(
            _stale_plan_source(source, "fixture.py"),
            [("fixture.py", "plan", 7)],
        )

    def test_a_with_body_non_plan_binding_invalidates_an_in_lock_plan(self):
        source = """
def repair(stale):
    with intent_transaction(footprint):
        plan = RendererMutationPlan.build()
        with nullcontext():
            plan = stale
        consume_renderer_plan(plan, permit)
"""

        self.assertEqual(
            _stale_plan_source(source, "fixture.py"),
            [("fixture.py", "plan", 7)],
        )

    def test_both_conditional_branches_can_build_an_in_lock_plan(self):
        source = """
def repair(rebuild):
    with intent_transaction(footprint):
        if rebuild:
            plan = RendererMutationPlan.build()
        else:
            plan = RendererMutationPlan.build()
        consume_renderer_plan(plan, permit)
"""

        self.assertEqual(_stale_plan_source(source, "fixture.py"), [])

    def test_a_helper_returning_an_obsolete_plan_binding_is_not_a_builder(self):
        source = """
def stale_plan(stale):
    plan = RendererMutationPlan.build()
    plan = stale
    return plan
def repair(stale):
    with intent_transaction(footprint):
        plan = stale_plan(stale)
        consume_renderer_plan(plan, permit)
"""

        self.assertEqual(
            _stale_plan_source(source, "fixture.py"),
            [("fixture.py", "plan", 9)],
        )

    def test_a_handler_sees_a_non_plan_binding_from_the_try_body(self):
        source = """
def repair(stale):
    with intent_transaction(footprint):
        plan = RendererMutationPlan.build()
        try:
            plan = stale
            raise ValueError()
        except ValueError:
            consume_renderer_plan(plan, permit)
"""

        self.assertEqual(
            _stale_plan_source(source, "fixture.py"),
            [("fixture.py", "plan", 9)],
        )

    def test_a_finally_consumer_sees_an_exception_after_a_nested_if_binding(self):
        source = """
def repair(stale, flag):
    with intent_transaction(footprint):
        plan = RendererMutationPlan.build()
        try:
            if flag:
                plan = stale
                prepare()
                plan = RendererMutationPlan.build()
        finally:
            consume_renderer_plan(plan, permit)
"""

        self.assertEqual(
            _stale_plan_source(source, "fixture.py"),
            [("fixture.py", "plan", 11)],
        )

    def test_a_finally_consumer_sees_an_exception_after_a_nested_with_binding(self):
        source = """
def repair(stale):
    with intent_transaction(footprint):
        plan = RendererMutationPlan.build()
        try:
            with nullcontext():
                plan = stale
                prepare()
                plan = RendererMutationPlan.build()
        finally:
            consume_renderer_plan(plan, permit)
"""

        self.assertEqual(
            _stale_plan_source(source, "fixture.py"),
            [("fixture.py", "plan", 11)],
        )

    def test_a_finally_consumer_sees_an_exception_after_a_nested_loop_binding(self):
        source = """
def repair(stale):
    with intent_transaction(footprint):
        plan = RendererMutationPlan.build()
        try:
            for item in items:
                plan = stale
                prepare(item)
                plan = RendererMutationPlan.build()
        finally:
            consume_renderer_plan(plan, permit)
"""

        self.assertEqual(
            _stale_plan_source(source, "fixture.py"),
            [("fixture.py", "plan", 11)],
        )

    def test_a_handler_consumer_sees_an_exception_after_a_nested_binding(self):
        source = """
def repair(stale, flag):
    with intent_transaction(footprint):
        plan = RendererMutationPlan.build()
        try:
            if flag:
                plan = stale
                prepare()
                plan = RendererMutationPlan.build()
        except:
            consume_renderer_plan(plan, permit)
"""

        self.assertEqual(
            _stale_plan_source(source, "fixture.py"),
            [("fixture.py", "plan", 11)],
        )

    def test_nested_exceptional_paths_accept_a_plan_repaired_before_consumption(self):
        sources = (
            """
def repair(stale, flag):
    with intent_transaction(footprint):
        plan = RendererMutationPlan.build()
        try:
            if flag:
                plan = stale
                prepare()
                plan = RendererMutationPlan.build()
        finally:
            plan = RendererMutationPlan.build()
            consume_renderer_plan(plan, permit)
""",
            """
def repair(flag):
    with intent_transaction(footprint):
        plan = RendererMutationPlan.build()
        try:
            if flag:
                prepare()
                plan = RendererMutationPlan.build()
        finally:
            consume_renderer_plan(plan, permit)
""",
        )

        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(_stale_plan_source(source, "fixture.py"), [])

    def test_a_finally_consumer_sees_an_early_try_exception(self):
        source = """
def repair(stale):
    with intent_transaction(footprint):
        plan = stale
        try:
            prepare()
            plan = RendererMutationPlan.build()
        finally:
            consume_renderer_plan(plan, permit)
"""

        self.assertEqual(
            _stale_plan_source(source, "fixture.py"),
            [("fixture.py", "plan", 9)],
        )

    def test_a_finally_consumer_sees_an_early_else_exception(self):
        source = """
def repair(stale):
    with intent_transaction(footprint):
        plan = stale
        try:
            prepare_body()
        except ValueError:
            plan = RendererMutationPlan.build()
        else:
            prepare_else()
            plan = RendererMutationPlan.build()
        finally:
            consume_renderer_plan(plan, permit)
"""

        self.assertEqual(
            _stale_plan_source(source, "fixture.py"),
            [("fixture.py", "plan", 13)],
        )

    def test_a_finally_consumer_sees_an_early_exception_group_handler_exception(self):
        source = """
def repair(stale):
    with intent_transaction(footprint):
        plan = stale
        try:
            prepare_body()
            plan = RendererMutationPlan.build()
        except* ValueError:
            prepare_handler()
            plan = RendererMutationPlan.build()
        finally:
            consume_renderer_plan(plan, permit)
"""

        self.assertEqual(
            _stale_plan_source(source, "fixture.py"),
            [("fixture.py", "plan", 12)],
        )

    def test_a_finally_consumer_accepts_a_plan_valid_on_every_entry(self):
        sources = (
            """
def repair():
    with intent_transaction(footprint):
        plan = RendererMutationPlan.build()
        try:
            prepare()
        finally:
            consume_renderer_plan(plan, permit)
""",
            """
def repair(stale):
    with intent_transaction(footprint):
        plan = stale
        try:
            prepare()
            plan = RendererMutationPlan.build()
        finally:
            plan = RendererMutationPlan.build()
            consume_renderer_plan(plan, permit)
""",
        )

        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(_stale_plan_source(source, "fixture.py"), [])

    def test_a_later_loop_iteration_sees_the_previous_non_plan_binding(self):
        source = """
def repair(stale):
    with intent_transaction(footprint):
        plan = RendererMutationPlan.build()
        for item in items:
            consume_renderer_plan(plan, permit)
            plan = stale
"""

        self.assertEqual(
            _stale_plan_source(source, "fixture.py"),
            [("fixture.py", "plan", 6)],
        )

    def test_a_continue_carries_a_non_plan_binding_to_the_next_iteration(self):
        source = """
def repair(stale, skip):
    with intent_transaction(footprint):
        plan = RendererMutationPlan.build()
        for item in items:
            consume_renderer_plan(plan, permit)
            plan = stale
            if skip:
                continue
            plan = RendererMutationPlan.build()
"""

        self.assertEqual(
            _stale_plan_source(source, "fixture.py"),
            [("fixture.py", "plan", 6)],
        )

    def test_a_continue_in_try_carries_a_non_plan_binding_to_the_next_iteration(self):
        source = """
def repair(stale):
    with intent_transaction(footprint):
        plan = RendererMutationPlan.build()
        for index in range(2):
            consume_renderer_plan(plan, permit)
            try:
                if index == 0:
                    plan = stale
                    continue
            finally:
                pass
            plan = RendererMutationPlan.build()
"""

        self.assertEqual(
            _stale_plan_source(source, "fixture.py"),
            [("fixture.py", "plan", 6)],
        )

    def test_a_break_carries_a_non_plan_binding_out_of_the_loop(self):
        source = """
def repair(stale):
    with intent_transaction(footprint):
        plan = RendererMutationPlan.build()
        for item in items:
            plan = stale
            break
            plan = RendererMutationPlan.build()
        else:
            plan = RendererMutationPlan.build()
        consume_renderer_plan(plan, permit)
"""

        self.assertEqual(
            _stale_plan_source(source, "fixture.py"),
            [("fixture.py", "plan", 11)],
        )

    def test_a_break_in_match_carries_a_non_plan_binding_out_of_the_loop(self):
        source = """
def repair(stale):
    with intent_transaction(footprint):
        plan = RendererMutationPlan.build()
        for item in items:
            match item:
                case _:
                    plan = stale
                    break
            plan = RendererMutationPlan.build()
        consume_renderer_plan(plan, permit)
"""

        self.assertEqual(
            _stale_plan_source(source, "fixture.py"),
            [("fixture.py", "plan", 11)],
        )

    def test_a_match_capture_replaces_a_plan_before_break(self):
        source = """
def repair():
    with intent_transaction(footprint):
        plan = RendererMutationPlan.build()
        for item in items:
            match item:
                case {"plan": plan}:
                    break
        consume_renderer_plan(plan, permit)
"""

        self.assertEqual(
            _stale_plan_source(source, "fixture.py"),
            [("fixture.py", "plan", 9)],
        )

    def test_a_match_capture_replaces_a_plan_before_the_consumer(self):
        source = """
def repair():
    with intent_transaction(footprint):
        plan = RendererMutationPlan.build()
        match value:
            case plan:
                consume_renderer_plan(plan, permit)
"""

        self.assertEqual(
            _stale_plan_source(source, "fixture.py"),
            [("fixture.py", "plan", 7)],
        )

    def test_a_failed_match_guard_carries_its_capture_to_a_later_case(self):
        source = """
def repair():
    with intent_transaction(footprint):
        plan = RendererMutationPlan.build()
        match value:
            case plan if False:
                pass
            case _:
                consume_renderer_plan(plan, permit)
"""

        self.assertEqual(
            _stale_plan_source(source, "fixture.py"),
            [("fixture.py", "plan", 9)],
        )

    def test_a_failed_match_guard_carries_its_capture_to_a_later_break(self):
        source = """
def repair():
    with intent_transaction(footprint):
        plan = RendererMutationPlan.build()
        for item in items:
            match item:
                case plan if False:
                    pass
                case _:
                    break
        consume_renderer_plan(plan, permit)
"""

        self.assertEqual(
            _stale_plan_source(source, "fixture.py"),
            [("fixture.py", "plan", 11)],
        )

    def test_a_short_circuited_match_guard_rebuild_does_not_certify_the_capture(self):
        source = """
def repair(flag):
    with intent_transaction(footprint):
        plan = RendererMutationPlan.build()
        match value:
            case plan if flag or (plan := RendererMutationPlan.build()):
                consume_renderer_plan(plan, permit)
"""

        self.assertEqual(
            _stale_plan_source(source, "fixture.py"),
            [("fixture.py", "plan", 7)],
        )

    def test_a_short_circuited_match_subject_rebuild_does_not_certify_the_plan(self):
        source = """
def repair(stale, flag):
    with intent_transaction(footprint):
        plan = stale
        match flag or (plan := RendererMutationPlan.build()):
            case _:
                consume_renderer_plan(plan, permit)
"""

        self.assertEqual(
            _stale_plan_source(source, "fixture.py"),
            [("fixture.py", "plan", 7)],
        )

    def test_a_match_guard_comprehension_can_invalidate_a_fresh_plan(self):
        source = """
def repair(stale):
    with intent_transaction(footprint):
        plan = RendererMutationPlan.build()
        match value:
            case _ if [plan := stale for _ in items]:
                consume_renderer_plan(plan, permit)
"""

        self.assertEqual(
            _stale_plan_source(source, "fixture.py"),
            [("fixture.py", "plan", 7)],
        )

    def test_a_helper_with_a_non_plan_return_path_is_not_a_builder(self):
        source = """
def maybe_plan(stale, fresh):
    if fresh:
        return RendererMutationPlan.build()
    return stale
def repair(stale, fresh):
    with intent_transaction(footprint):
        plan = maybe_plan(stale, fresh)
        consume_renderer_plan(plan, permit)
"""

        self.assertEqual(
            _stale_plan_source(source, "fixture.py"),
            [("fixture.py", "plan", 9)],
        )

    def test_a_helper_with_an_implicit_non_plan_return_is_not_a_builder(self):
        source = """
def maybe_plan(fresh):
    if fresh:
        return RendererMutationPlan.build()
def repair(fresh):
    with intent_transaction(footprint):
        plan = maybe_plan(fresh)
        consume_renderer_plan(plan, permit)
"""

        self.assertEqual(
            _stale_plan_source(source, "fixture.py"),
            [("fixture.py", "plan", 8)],
        )

    def test_a_helper_with_a_bare_return_is_not_a_builder(self):
        source = """
def maybe_plan(empty):
    if empty:
        return
    return RendererMutationPlan.build()
def repair(empty):
    with intent_transaction(footprint):
        plan = maybe_plan(empty)
        consume_renderer_plan(plan, permit)
"""

        self.assertEqual(
            _stale_plan_source(source, "fixture.py"),
            [("fixture.py", "plan", 9)],
        )

    def test_a_mixed_tuple_helper_certifies_only_its_plan_positions(self):
        source = """
def mixed(stale):
    return RendererMutationPlan.build(), stale
def repair(stale):
    with intent_transaction(footprint):
        fresh, plan = mixed(stale)
        consume_renderer_plan(plan, permit)
"""

        self.assertEqual(
            _stale_plan_source(source, "fixture.py"),
            [("fixture.py", "plan", 7)],
        )

    def test_a_nested_mixed_tuple_helper_certifies_only_its_plan_positions(self):
        source = """
def mixed(stale):
    return (RendererMutationPlan.build(), stale), None
def repair(stale):
    with intent_transaction(footprint):
        (fresh, plan), ignored = mixed(stale)
        consume_renderer_plan(plan, permit)
"""

        self.assertEqual(
            _stale_plan_source(source, "fixture.py"),
            [("fixture.py", "plan", 7)],
        )

    def test_a_conditional_plan_expression_requires_two_plan_paths(self):
        source = """
def repair(stale, fresh):
    with intent_transaction(footprint):
        plan = RendererMutationPlan.build() if fresh else stale
        consume_renderer_plan(plan, permit)
"""

        self.assertEqual(
            _stale_plan_source(source, "fixture.py"),
            [("fixture.py", "plan", 5)],
        )

    def test_a_nested_lock_requires_a_plan_built_under_that_lock(self):
        source = """
def repair():
    with intent_transaction(outer_footprint):
        plan = RendererMutationPlan.build()
        with intent_transaction(inner_footprint):
            consume_renderer_plan(plan, permit)
"""

        self.assertEqual(
            _stale_plan_source(source, "fixture.py"),
            [("fixture.py", "plan", 6)],
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

    def test_returning_a_lambda_does_not_make_a_function_a_plan_builder(self):
        deferred_source = """
def deferred():
    plan = RendererMutationPlan.build()
    return lambda: plan
def repair():
    with intent_transaction(footprint):
        plan = deferred()
        consume_renderer_plan(plan, permit)
"""
        self.assertEqual(
            _stale_plan_source(deferred_source, "fixture.py"),
            [("fixture.py", "plan", 8)],
        )

        direct_source = deferred_source.replace("return lambda: plan", "return plan")
        self.assertEqual(_stale_plan_source(direct_source, "fixture.py"), [])

        default_source = """
def deferred():
    return lambda p=RendererMutationPlan.build(): p
def repair():
    with intent_transaction(footprint):
        plan = deferred()
        consume_renderer_plan(plan, permit)
"""
        self.assertEqual(
            _stale_plan_source(default_source, "fixture.py"),
            [("fixture.py", "plan", 7)],
        )

    def test_a_lambda_valued_assignment_is_not_a_plan(self):
        source = """
def repair():
    with intent_transaction(footprint):
        plan = lambda p=RendererMutationPlan.build(): p
        consume_renderer_plan(plan, permit)
"""

        self.assertEqual(
            _stale_plan_source(source, "fixture.py"),
            [("fixture.py", "plan", 5)],
        )

        direct_source = source.replace("lambda p=RendererMutationPlan.build(): p", "RendererMutationPlan.build()")
        self.assertEqual(_stale_plan_source(direct_source, "fixture.py"), [])

    def test_lambda_branches_do_not_make_a_conditional_assignment_a_plan(self):
        source = """
def repair():
    with intent_transaction(footprint):
        plan = (lambda p=RendererMutationPlan.build(): p) if flag else (lambda: None)
        consume_renderer_plan(plan, permit)
"""

        self.assertEqual(
            _stale_plan_source(source, "fixture.py"),
            [("fixture.py", "plan", 5)],
        )

    def test_returning_lambda_branches_does_not_make_a_function_a_plan_builder(self):
        source = """
def deferred(flag):
    return (lambda p=RendererMutationPlan.build(): p) if flag else (lambda: None)
def repair():
    with intent_transaction(footprint):
        plan = deferred(flag)
        consume_renderer_plan(plan, permit)
"""

        self.assertEqual(
            _stale_plan_source(source, "fixture.py"),
            [("fixture.py", "plan", 7)],
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
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            if _consumers(tree):
                modules.add(relative)
                builders[relative] = sorted(set(_plan_builders(tree)) - {_PLAN_BUILDER})

        self.assertEqual(sorted(modules), ["ownership_planner.py", "renderer_audit.py"])
        self.assertIn("_demotion_plan", builders["ownership_planner.py"])
        self.assertIn("_repair_plan", builders["renderer_audit.py"])
