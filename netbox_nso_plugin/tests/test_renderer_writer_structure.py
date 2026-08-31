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
        ("apply_state.py", "promote_current_intent", "locked.save"),
        ("drain.py", "_stamp_last_acked", "NSOStaticRouteState.objects.bulk_update"),
        ("drain.py", "clear_acknowledged_lineage", "NSOStaticRouteState.objects.exclude().update"),
        ("link_role.py", "apply_description_for_role", "NSOInterfaceState.objects.update_or_create"),
        ("link_role.py", "enable_igp_for_role", "NSOISISInterfaceState.objects.update_or_create"),
        ("link_role.py", "enable_igp_for_role", "NSOOSPFInterfaceState.objects.update_or_create"),
        ("onboarding.py", "onboard_candidate", "NSOPlatformNedMapping.objects.get_or_create"),
        ("signals.py", "_create_greenfield_subif_state", "NSOSubinterfaceState.objects.create"),
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
