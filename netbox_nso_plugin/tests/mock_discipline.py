# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""AST 'mock discipline' guard — flag attribute-fabricating mocks used as object stand-ins.

A bare ``MagicMock()`` (or ``Mock()``) synthesises *any* attribute or method on demand,
so a test built on one stays green while the real code path is broken — e.g. a field the
production branch reads is never actually set, yet ``row.whatever`` still returns a truthy
mock. Mocks are a last resort reserved for true external boundaries you cannot run locally
(third-party network calls, paid/destructive/nondeterministic side effects), and even there
a ``spec=``-bounded mock or a real fake (``httpx.MockTransport``) beats a bare one.

This scanner is deliberately a bit too aggressive: it flags every instantiation of a
fabricating mock class, then lets you carve out the legitimate cases three ways —

  1. **Bound it.** ``MagicMock(spec=NsoClient)`` / ``spec_set=`` / ``create_autospec`` /
     ``wraps=real_obj`` restrict (or delegate) attribute access to a real interface, so the
     fabrication footgun is gone. These are never flagged.
  2. **Mark it.** An inline ``# mock-ok: <reason>`` comment on the statement records a
     reviewed, deliberate boundary mock. Preferred for new code — it documents *why*.
  3. **Grandfather it.** ``tests/mock_discipline_baseline.txt`` records the count of
     currently-accepted spec-less mocks per (file, function). New ones beyond the recorded
     count fail the guard. Regenerate after an intentional change with::

         python netbox_nso_plugin/tests/mock_discipline.py --update-baseline

``AsyncMock`` gets a narrower rule. A bare ``AsyncMock()`` standing in for a single awaitable
(a coroutine/function stub or a spy you ``assert``-on) is idiomatic and exempt — there is no
object interface to ``spec=``. But an ``AsyncMock`` used as an *object* stand-in — one the test
configures with **two or more distinct attributes** (``client.list_devices = ...;
client.get_interface = ...``) — is the same fabrication footgun as a bare ``MagicMock`` and
**is** flagged (``FLAG_OBJECT_SHAPED_ASYNCMOCK``); bind it ``AsyncMock(spec=NetboxClient)``.
Set ``INCLUDE_ASYNCMOCK = True`` to additionally flag *every* AsyncMock (noisy; off by
default). Tune the policy by editing the constants below as the suite evolves.
"""

from __future__ import annotations

import ast
import io
import tokenize
from dataclasses import dataclass
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parent
_BASELINE_PATH = TESTS_ROOT / "mock_discipline_baseline.txt"

# Mock classes that fabricate arbitrary attributes when unspecced — the dangerous kind.
_FABRICATING_MOCKS = {"MagicMock", "NonCallableMagicMock", "Mock", "NonCallableMock"}
# Flip to also flag *every* AsyncMock at its call site (idiomatic for awaitable stubs — noisy).
INCLUDE_ASYNCMOCK = False
# Flag an AsyncMock used as an OBJECT stand-in: assigned to a name that then receives this
# many or more distinct attribute assignments (so a renamed method on the faked interface
# would be silently fabricated). A 0/1-attribute AsyncMock (pure callable/spy) stays exempt.
FLAG_OBJECT_SHAPED_ASYNCMOCK = True
_OBJECT_SHAPE_MIN_ATTRS = 2
# Keyword args that bound a mock to a real interface (or delegate to a real object).
_BOUNDING_KWARGS = {"spec", "spec_set", "autospec", "wraps"}
# Inline opt-out marker (in a comment): `# mock-ok` or `# mock-ok: reason`.
_MARKER = "mock-ok"
# Files the scanner never inspects (itself + its own test).
_SELF = {"mock_discipline.py", "test_mock_discipline.py"}


def _targets() -> set[str]:
    return _FABRICATING_MOCKS | ({"AsyncMock"} if INCLUDE_ASYNCMOCK else set())


@dataclass(frozen=True)
class Violation:
    """One flagged mock instantiation."""

    path: str  # posix relpath from tests/
    lineno: int
    qualname: str  # enclosing function/class path, or "<module>"
    mock: str  # the mock class name

    @property
    def site(self) -> str:
        """Stable (line-independent) key used by the baseline: file + enclosing scope."""
        return f"{self.path}::{self.qualname}"

    def __str__(self) -> str:
        return f"{self.path}:{self.lineno}: unapproved {self.mock}() in {self.qualname}()"


def _comment_lines(src: str) -> dict[int, str]:
    """Map line-number → comment text for every real comment token (string-safe)."""
    comments: dict[int, str] = {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                comments[tok.start[0]] = tok.string
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return comments


def _walk_own_scope(node: ast.AST):
    """Yield descendants of *node* in its OWN lexical scope — i.e. stop at (do not descend
    into) nested function/class definitions, which are analysed as their own scopes."""
    for child in ast.iter_child_nodes(node):
        yield child
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield from _walk_own_scope(child)


def _mock_import_aliases(tree: ast.AST) -> dict[str, str]:
    """Local-name → canonical-class for ``from unittest.mock import MagicMock [as MM]``."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[-1] == "mock":
            for alias in node.names:
                aliases[alias.asname or alias.name] = alias.name
    return aliases


class _Scanner(ast.NodeVisitor):
    """Collect fabricating-mock instantiations that are neither bounded nor marked."""

    def __init__(self, rel: str, comments: dict[int, str], aliases: dict[str, str]):
        self._rel = rel
        self._comments = comments
        self._aliases = aliases
        self._scope: list[str] = []
        self.hits: list[Violation] = []

    # ── scope tracking ────────────────────────────────────────────────────────
    def _qual(self) -> str:
        return ".".join(self._scope) or "<module>"

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._scope.append(node.name)
        self._collect_object_shaped_async(node)
        self.generic_visit(node)
        self._scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    # ── the check ─────────────────────────────────────────────────────────────
    def visit_Call(self, node: ast.Call) -> None:
        name = self._mock_class(node.func)
        if name and not self._is_bounded(node) and not self._is_marked(node):
            self.hits.append(Violation(self._rel, node.lineno, self._qual(), name))
        self.generic_visit(node)

    def _mock_class(self, func: ast.expr) -> str | None:
        targets = _targets()
        if isinstance(func, ast.Attribute) and func.attr in targets:
            return func.attr  # e.g. mock.MagicMock(...), unittest.mock.MagicMock(...)
        if isinstance(func, ast.Name):
            canonical = self._aliases.get(func.id)
            if canonical in targets:
                return canonical  # imported (possibly aliased) name
        return None

    # ── object-shaped AsyncMock check ───────────────────────────────────────────
    def _collect_object_shaped_async(self, fn: ast.AST) -> None:
        """Flag a spec-less ``AsyncMock()`` bound to a name that, within this same scope,
        receives ``_OBJECT_SHAPE_MIN_ATTRS`` or more distinct attribute assignments — it is
        being used as a multi-method object stand-in, not a single awaitable stub."""
        if INCLUDE_ASYNCMOCK or not FLAG_OBJECT_SHAPED_ASYNCMOCK:
            return  # blanket mode already covers AsyncMock, or the rule is disabled
        assigns: dict[str, ast.Call] = {}  # var name → the AsyncMock() call it's bound to
        attrs: dict[str, set[str]] = {}  # var name → distinct attribute names assigned on it
        for n in _walk_own_scope(fn):
            if not isinstance(n, ast.Assign) or len(n.targets) != 1:
                continue
            target = n.targets[0]
            if isinstance(target, ast.Name) and self._is_unbounded_asyncmock(n.value):
                assigns[target.id] = n.value
            elif isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
                attrs.setdefault(target.value.id, set()).add(target.attr)
        for name, call in assigns.items():
            if len(attrs.get(name, ())) >= _OBJECT_SHAPE_MIN_ATTRS:
                self.hits.append(Violation(self._rel, call.lineno, self._qual(), "AsyncMock"))

    def _is_unbounded_asyncmock(self, node: ast.expr) -> bool:
        if not isinstance(node, ast.Call):
            return False
        func = node.func
        name = (
            func.attr
            if isinstance(func, ast.Attribute)
            else (self._aliases.get(func.id) if isinstance(func, ast.Name) else None)
        )
        return name == "AsyncMock" and not self._is_bounded(node) and not self._is_marked(node)

    @staticmethod
    def _is_bounded(node: ast.Call) -> bool:
        return any(kw.arg in _BOUNDING_KWARGS for kw in node.keywords)

    def _is_marked(self, node: ast.Call) -> bool:
        # A `# mock-ok` marker counts if it's a trailing/inline comment anywhere in the
        # call's own line span, or in a contiguous comment block directly above the line
        # (so the reason can be written above the mock, the way people naturally do).
        end = getattr(node, "end_lineno", node.lineno) or node.lineno
        if any(_MARKER in self._comments.get(ln, "") for ln in range(node.lineno, end + 1)):
            return True
        ln = node.lineno - 1
        while ln in self._comments:
            if _MARKER in self._comments[ln]:
                return True
            ln -= 1
        return False


def scan_source(src: str, rel: str = "<source>") -> list[Violation]:
    """Scan one module's source text and return its mock-discipline violations."""
    tree = ast.parse(src, filename=rel)
    scanner = _Scanner(rel, _comment_lines(src), _mock_import_aliases(tree))
    scanner.visit(tree)
    return scanner.hits


def scan_tree(root: Path = TESTS_ROOT) -> list[Violation]:
    """Scan every test module under *root* (skipping the guard's own files)."""
    out: list[Violation] = []
    for path in sorted(root.rglob("*.py")):
        if path.name in _SELF or "__pycache__" in path.parts:
            continue
        rel = path.relative_to(root).as_posix()
        out.extend(scan_source(path.read_text(encoding="utf-8"), rel))
    return out


def _counts_by_site(violations: list[Violation]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for v in violations:
        counts[v.site] = counts.get(v.site, 0) + 1
    return counts


def load_baseline(path: Path = _BASELINE_PATH) -> dict[str, int]:
    """Read the grandfathered per-site allowance (``site\\tcount`` lines)."""
    if not path.exists():
        return {}
    allowed: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        site, _, count = line.rpartition("\t")
        allowed[site] = int(count)
    return allowed


def save_baseline(counts: dict[str, int], path: Path = _BASELINE_PATH) -> None:
    """Write the per-site allowance file (sorted, with an explanatory header)."""
    # REUSE-IgnoreStart — these are the *generated baseline's* own header strings, not this
    # module's license; the real one is at the top of the file. REUSE must not parse them.
    header = [
        "# SPDX-License-Identifier: Apache-2.0",
        "# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>",
        "# Mock-discipline baseline — grandfathered spec-less MagicMock/Mock (and object-",
        "# shaped AsyncMock) usages.",
        "# Each line: <relpath-from-tests>::<qualname>\\t<allowed-count>.",
        "# Shrink this file over time: replace a mock with a real object or a spec=-bounded",
        "# mock, or add an inline `# mock-ok: <reason>`. Regenerate after an intentional",
        "# change with:  python netbox_nso_plugin/tests/mock_discipline.py --update-baseline",
        "",
    ]
    # REUSE-IgnoreEnd
    body = [f"{site}\t{counts[site]}" for site in sorted(counts)]
    path.write_text("\n".join(header + body) + "\n", encoding="utf-8")


def unapproved(root: Path = TESTS_ROOT, baseline: dict[str, int] | None = None) -> list[Violation]:
    """Return violations beyond the baseline allowance, sorted by file then line."""
    allowed = load_baseline() if baseline is None else baseline
    by_site: dict[str, list[Violation]] = {}
    for v in scan_tree(root):
        by_site.setdefault(v.site, []).append(v)
    extra: list[Violation] = []
    for site, found in by_site.items():
        budget = allowed.get(site, 0)
        if len(found) > budget:
            # Report the excess (the newest-by-line ones beyond the grandfathered count).
            extra.extend(sorted(found, key=lambda v: v.lineno)[budget:])
    return sorted(extra, key=lambda v: (v.path, v.lineno))


def _main(argv: list[str]) -> int:
    if "--update-baseline" in argv:
        counts = _counts_by_site(scan_tree())
        save_baseline(counts)
        print(f"baseline updated: {sum(counts.values())} mock(s) grandfathered across {len(counts)} site(s)")
        return 0
    bad = unapproved()
    for v in bad:
        print(str(v))
    print(f"\n{len(bad)} unapproved mock(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
