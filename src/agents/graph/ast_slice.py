"""Deterministic sink-anchored backward syntactic slice (tree-sitter, LLM-free).

Given the full source and a 1-based sink line, this module resolves every
identifier the sink transitively depends on — scope-chain declaration lookup
(innermost first, so shadowing is exact), same-scope writes (post-sink writes
included: loop iterations can feed the sink), cross-scope writes for
fields/globals — expanded to a fixpoint capped by ``SLICE_MAX_FACTS``. Names
with no declaration or write anywhere in the file are kept as negative facts
("not declared in this file"); Phase 1 exit routing depends on them.

The slice states *syntactic* kinds only (parameter/local/field/global/function/
class/import). Semantic source classification (external_input, constant, ...)
stays with the Phase 1 LLM.

Fail-open contract: a missing or broken grammar returns ``None``; a sink line
that yields neither identifiers nor an enclosing scope produces a slice whose
``is_degenerate()`` is true. Both route the caller to the lexical-trace
fallback. ``tree.root_node.has_error`` is deliberately *not* a fallback
trigger: on real data (GV1, 2026-07-10) C++ files parse with local errors in
46% of cases while slice resolution still succeeds with zero misses.

All public line numbers are 1-based (tree-sitter rows are 0-based, corrected
here, matching the ``chunking`` convention).
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Final

from agents.graph.guards import estimate_tokens

try:
    from agents.config import SLICE_MAX_FACTS, SLICE_TOKEN_LIMIT
except ImportError:  # pragma: no cover - obfuscated builds keep fallbacks in sync
    SLICE_TOKEN_LIMIT: Final = 500
    SLICE_MAX_FACTS: Final = 25

_SNIPPET_CHARS: Final = 80
_CHAIN_RENDER_LIMIT: Final = 3
_DECL_RENDER_LIMIT: Final = 2
_WRITE_RENDER_LIMIT: Final = 3
_CROSS_RENDER_LIMIT: Final = 2
_CHARS_PER_TOKEN: Final = 4

_GRAMMAR_MODULES: Final = {
    "python": "tree_sitter_python",
    "java": "tree_sitter_java",
    "typescript": "tree_sitter_typescript",
    "javascript": "tree_sitter_javascript",
    "go": "tree_sitter_go",
    "c": "tree_sitter_c",
    "cpp": "tree_sitter_cpp",
}

# Scope node types that classify an enclosed declaration as field vs global.
_CLASS_LIKE_SCOPES: Final = frozenset(
    {
        "class_declaration",
        "interface_declaration",
        "enum_declaration",
        "class_definition",
        "class_specifier",
        "struct_specifier",
        "namespace_definition",
        "class",
    }
)
_ROOT_SCOPES: Final = frozenset({"program", "module", "source_file", "translation_unit"})


@dataclass(frozen=True, slots=True)
class _Table:
    """Per-language tree-sitter node-type tables (validated by gate GV1)."""

    scopes: frozenset[str]
    decls: frozenset[str]
    assigns: frozenset[str]
    idents: frozenset[str]
    param_decls: frozenset[str] = frozenset()
    param_containers: frozenset[str] = frozenset()
    import_decls: frozenset[str] = frozenset()
    import_containers: frozenset[str] = frozenset()
    # member-access node type -> (member field, receiver field)
    member_parent: dict[str, tuple[str, str]] | None = None
    self_names: frozenset[str] = frozenset()
    # scope node type -> field holding the name it declares in the outer scope
    scope_names: dict[str, str] | None = None
    # node type -> field whose identifiers are parameters (bare lambda params)
    param_overrides: dict[str, str] | None = None


_TABLES: Final[dict[str, _Table]] = {
    "python": _Table(
        scopes=frozenset({"function_definition", "class_definition", "lambda", "module"}),
        decls=frozenset(
            {
                "typed_parameter",
                "default_parameter",
                "typed_default_parameter",
                "as_pattern",
                "named_expression",
                "aliased_import",
                "for_statement",
                "except_clause",
                "global_statement",
            }
        ),
        assigns=frozenset({"assignment", "augmented_assignment"}),
        idents=frozenset({"identifier"}),
        param_decls=frozenset({"typed_parameter", "default_parameter", "typed_default_parameter"}),
        param_containers=frozenset({"parameters", "lambda_parameters"}),
        import_decls=frozenset({"aliased_import"}),
        import_containers=frozenset({"import_statement", "import_from_statement"}),
        member_parent={"attribute": ("attribute", "object")},
        self_names=frozenset({"self", "cls"}),
        scope_names={"function_definition": "name", "class_definition": "name"},
    ),
    "java": _Table(
        scopes=frozenset(
            {
                "method_declaration",
                "constructor_declaration",
                "class_declaration",
                "interface_declaration",
                "enum_declaration",
                "lambda_expression",
                "static_initializer",
                "program",
            }
        ),
        decls=frozenset(
            {
                "variable_declarator",
                "formal_parameter",
                "spread_parameter",
                "catch_formal_parameter",
                "enhanced_for_statement",
            }
        ),
        assigns=frozenset({"assignment_expression"}),
        idents=frozenset({"identifier"}),
        param_decls=frozenset({"formal_parameter", "spread_parameter", "catch_formal_parameter"}),
        param_containers=frozenset({"inferred_parameters"}),
        member_parent={
            "field_access": ("field", "object"),
            "method_invocation": ("name", "object"),
        },
        self_names=frozenset({"this"}),
        scope_names={
            "method_declaration": "name",
            "constructor_declaration": "name",
            "class_declaration": "name",
            "interface_declaration": "name",
            "enum_declaration": "name",
        },
        param_overrides={"lambda_expression": "parameters"},
    ),
    "typescript": _Table(
        scopes=frozenset(
            {
                "function_declaration",
                "function_expression",
                "arrow_function",
                "method_definition",
                "class_declaration",
                "class",
                "generator_function",
                "generator_function_declaration",
                "program",
            }
        ),
        decls=frozenset(
            {
                "variable_declarator",
                "required_parameter",
                "optional_parameter",
                "catch_clause",
                "import_specifier",
                "namespace_import",
                "import_clause",
            }
        ),
        assigns=frozenset({"assignment_expression", "augmented_assignment_expression"}),
        idents=frozenset({"identifier", "shorthand_property_identifier"}),
        param_decls=frozenset({"required_parameter", "optional_parameter"}),
        param_containers=frozenset({"formal_parameters"}),
        import_decls=frozenset({"import_specifier", "namespace_import", "import_clause"}),
        member_parent={"member_expression": ("property", "object")},
        self_names=frozenset({"this"}),
        scope_names={
            "function_declaration": "name",
            "class_declaration": "name",
            "method_definition": "name",
            "function_expression": "name",
            "generator_function_declaration": "name",
        },
        param_overrides={"arrow_function": "parameter"},
    ),
    "javascript": _Table(
        scopes=frozenset(
            {
                "function_declaration",
                "function_expression",
                "arrow_function",
                "method_definition",
                "class_declaration",
                "class",
                "generator_function",
                "generator_function_declaration",
                "program",
            }
        ),
        decls=frozenset(
            {
                "variable_declarator",
                "catch_clause",
                "import_specifier",
                "namespace_import",
                "import_clause",
            }
        ),
        assigns=frozenset({"assignment_expression", "augmented_assignment_expression"}),
        idents=frozenset({"identifier", "shorthand_property_identifier"}),
        param_containers=frozenset({"formal_parameters"}),
        import_decls=frozenset({"import_specifier", "namespace_import", "import_clause"}),
        member_parent={"member_expression": ("property", "object")},
        self_names=frozenset({"this"}),
        scope_names={
            "function_declaration": "name",
            "class_declaration": "name",
            "method_definition": "name",
            "function_expression": "name",
            "generator_function_declaration": "name",
        },
        param_overrides={"arrow_function": "parameter"},
    ),
    "go": _Table(
        scopes=frozenset(
            {"function_declaration", "method_declaration", "func_literal", "source_file"}
        ),
        decls=frozenset(
            {
                "short_var_declaration",
                "var_spec",
                "const_spec",
                "parameter_declaration",
                "variadic_parameter_declaration",
                "range_clause",
                "receive_statement",
                "import_spec",
            }
        ),
        assigns=frozenset({"assignment_statement"}),
        # field_identifier: method names in method_declaration
        idents=frozenset({"identifier", "field_identifier"}),
        param_decls=frozenset({"parameter_declaration", "variadic_parameter_declaration"}),
        import_decls=frozenset({"import_spec"}),
        import_containers=frozenset({"import_declaration"}),
        member_parent={"selector_expression": ("field", "operand")},
        scope_names={"function_declaration": "name", "method_declaration": "name"},
    ),
    "c": _Table(
        scopes=frozenset({"function_definition", "translation_unit"}),
        decls=frozenset(
            {
                "init_declarator",
                "declaration",
                "parameter_declaration",
                "preproc_def",
                "preproc_function_def",
            }
        ),
        assigns=frozenset({"assignment_expression"}),
        idents=frozenset({"identifier"}),
        param_decls=frozenset({"parameter_declaration"}),
        member_parent={"field_expression": ("field", "argument")},
        scope_names={"function_definition": "declarator"},
    ),
    "cpp": _Table(
        scopes=frozenset(
            {
                "function_definition",
                "lambda_expression",
                "class_specifier",
                "struct_specifier",
                "namespace_definition",
                "translation_unit",
            }
        ),
        decls=frozenset(
            {
                "init_declarator",
                "declaration",
                "parameter_declaration",
                "optional_parameter_declaration",
                "field_declaration",
                "structured_binding_declarator",
                "for_range_loop",
                "preproc_def",
                "preproc_function_def",
            }
        ),
        assigns=frozenset({"assignment_expression"}),
        # field_identifier: in-class method and field names
        idents=frozenset({"identifier", "field_identifier"}),
        param_decls=frozenset({"parameter_declaration", "optional_parameter_declaration"}),
        member_parent={"field_expression": ("field", "argument")},
        self_names=frozenset({"this"}),
        scope_names={
            "function_definition": "declarator",
            "class_specifier": "name",
            "struct_specifier": "name",
            "namespace_definition": "name",
        },
    ),
}

# Definition-side name lookup: first present field wins; otherwise the node is
# walked with its value-side field subtrees excluded.
_NAME_FIELDS: Final = ("name", "declarator", "left", "alias", "pattern")
_VALUE_FIELDS: Final = (
    "value",
    "right",
    "init",
    "type",
    "arguments",
    "condition",
    "body",
    "parameters",
)
_RHS_FIELDS: Final = ("value", "right", "init")


# --------------------------------------------------------------------------- #
# Public data model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class SourceRef:
    """One 1-based location fact about an identifier."""

    line: int
    kind: str  # parameter | local | field | global | function | class | import | write | cross_write
    text: str


@dataclass(frozen=True, slots=True)
class IdentifierFact:
    name: str
    decls: tuple[SourceRef, ...] = ()
    writes: tuple[SourceRef, ...] = ()
    cross_writes: tuple[SourceRef, ...] = ()

    @property
    def resolved(self) -> bool:
        return bool(self.decls or self.writes or self.cross_writes)


@dataclass(frozen=True, slots=True)
class ScopeRef:
    kind: str  # tree-sitter scope node type
    line: int
    text: str
    name: str = ""  # declared name of the scope (empty for anonymous scopes)


@dataclass(frozen=True, slots=True)
class SinkSlice:
    language: str
    sink_line: int
    enclosing_chain: tuple[ScopeRef, ...] = ()
    facts: tuple[IdentifierFact, ...] = ()

    def is_degenerate(self) -> bool:
        """True when the slice carries nothing — route to the lexical fallback."""
        return not self.facts and not self.enclosing_chain


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def build_sink_slice(
    code: str,
    language: str,
    sink_line: int,
    *,
    file_extension: str = "",
) -> SinkSlice | None:
    """Build the backward slice for ``sink_line``; ``None`` when no grammar loads."""
    table = _TABLES.get(language)
    if table is None:
        return None
    try:
        grammar = _load_language(language, file_extension)
        from tree_sitter import Parser

        tree = Parser(grammar).parse(code.encode("utf-8", errors="replace"))
    except (ImportError, OSError, ValueError, RuntimeError):
        return None

    code_lines = code.splitlines()
    if not (0 < sink_line <= len(code_lines)):
        return SinkSlice(language=language, sink_line=sink_line)
    builder = _SliceBuilder(table, tree, code, code_lines, sink_line)
    return SinkSlice(
        language=language,
        sink_line=sink_line,
        enclosing_chain=builder.enclosing_chain(),
        facts=builder.facts(),
    )


def render_sink_slice(slice_: SinkSlice, *, max_tokens: int = SLICE_TOKEN_LIMIT) -> str:
    """Token-capped textual rendering; negative facts stay explicit."""
    parts: list[str] = []
    if slice_.enclosing_chain:
        parts.append(
            "enclosing: "
            + " > ".join(
                f"{ref.kind} L{ref.line}: {ref.text}"
                for ref in slice_.enclosing_chain[:_CHAIN_RENDER_LIMIT]
            )
        )
    parts.extend(render_identifier_fact(fact) for fact in slice_.facts)
    text = "\n".join(parts)
    if estimate_tokens(text) > max_tokens:
        text = text[: max_tokens * _CHARS_PER_TOKEN] + "\n(truncated)"
    return text


def render_identifier_fact(fact: IdentifierFact) -> str:
    """One ``name: facts`` line (negative fact when nothing resolved)."""
    decl_lines = {ref.line for ref in fact.decls}
    segments = [
        f"declared L{ref.line} ({ref.kind}): {ref.text}"
        for ref in fact.decls[:_DECL_RENDER_LIMIT]
    ]
    segments += [
        f"written L{ref.line}: {ref.text}"
        for ref in fact.writes
        if ref.line not in decl_lines
    ][:_WRITE_RENDER_LIMIT]
    segments += [
        f"written(other scope) L{ref.line}: {ref.text}"
        for ref in fact.cross_writes[:_CROSS_RENDER_LIMIT]
    ]
    if not segments:
        segments = ["not declared in this file"]
    return f"{fact.name}: " + "; ".join(segments)


def resolve_identifier(
    code: str,
    language: str,
    name: str,
    *,
    near_line: int = 1,
    file_extension: str = "",
) -> IdentifierFact | None:
    """Scope-resolve one identifier by name (``None`` when no grammar loads).

    The occurrence nearest to ``near_line`` anchors the scope chain; a name
    with no occurrence at all is resolved from the file scope, so the returned
    fact still carries declarations/writes when they exist and stays an
    explicit negative fact otherwise.
    """
    table = _TABLES.get(language)
    if table is None:
        return None
    try:
        grammar = _load_language(language, file_extension)
        from tree_sitter import Parser

        tree = Parser(grammar).parse(code.encode("utf-8", errors="replace"))
    except (ImportError, OSError, ValueError, RuntimeError):
        return None
    code_lines = code.splitlines()
    builder = _SliceBuilder(table, tree, code, code_lines, max(1, min(near_line, len(code_lines) or 1)))
    occurrence = builder.find_occurrence(name, near_line) or tree.root_node
    decls, writes, cross_writes = builder._resolve(name, occurrence)
    return IdentifierFact(
        name=name,
        decls=builder._decl_refs(decls),
        writes=builder._write_refs(writes, "write"),
        cross_writes=builder._write_refs(cross_writes, "cross_write"),
    )


_LANGUAGE_CACHE: dict[tuple[str, bool], object] = {}


def _load_language(language: str, file_extension: str = ""):
    """Load (and cache) the tree-sitter grammar for a supported language."""
    tsx = language == "typescript" and file_extension.lower() == ".tsx"
    key = (language, tsx)
    grammar = _LANGUAGE_CACHE.get(key)
    if grammar is None:
        from tree_sitter import Language

        module = importlib.import_module(_GRAMMAR_MODULES[language])
        if language == "typescript":
            raw = module.language_tsx() if tsx else module.language_typescript()
        else:
            raw = module.language()
        grammar = Language(raw)
        _LANGUAGE_CACHE[key] = grammar
    return grammar


# --------------------------------------------------------------------------- #
# Builder
# --------------------------------------------------------------------------- #
class _SliceBuilder:
    """One-pass index over the tree, then scope-chain resolution to a fixpoint."""

    def __init__(self, table: _Table, tree, code: str, code_lines: list[str], sink_line: int):
        self.table = table
        self.tree = tree
        self.src_bytes = code.encode("utf-8", errors="replace")
        self.code_lines = code_lines
        self.sink_line = sink_line
        # name -> [(node, kind_hint | None)]
        self.decl_index: dict[str, list[tuple[object, str | None]]] = {}
        self.assign_index: dict[str, list[object]] = {}
        self.sink_nodes: dict[str, object] = {}
        self._index()

    # -- text helpers -------------------------------------------------------
    def _text(self, node) -> str:
        return self.src_bytes[node.start_byte : node.end_byte].decode(
            "utf-8", errors="replace"
        )

    def _line(self, node) -> int:
        return node.start_point[0] + 1  # tree-sitter rows are 0-based

    def _snippet(self, line: int) -> str:
        if 0 < line <= len(self.code_lines):
            return self.code_lines[line - 1].strip()[:_SNIPPET_CHARS]
        return ""

    # -- indexing -------------------------------------------------------------
    def _index(self) -> None:
        table = self.table
        sink_row = self.sink_line - 1
        scope_names = table.scope_names or {}
        param_overrides = table.param_overrides or {}
        sink_hits: list[tuple[int, str, object]] = []
        stack = [self.tree.root_node]
        while stack:
            node = stack.pop()
            node_type = node.type
            if node_type in scope_names:
                kind = "class" if node_type in _CLASS_LIKE_SCOPES else "function"
                for child in node.children_by_field_name(scope_names[node_type]):
                    for name in self._definition_names(child):
                        self._add_decl(name, node, kind)
            if node_type in param_overrides:
                for child in node.children_by_field_name(param_overrides[node_type]):
                    for name in self._idents_below(child):
                        self._add_decl(name, node, "parameter")
            if node_type in table.idents:
                name = self._text(node)
                if name:
                    if node.start_point[0] == sink_row and self._readable(node):
                        sink_hits.append((node.start_point[1], name, node))
                    parent = node.parent
                    if parent is not None and parent.type in table.param_containers:
                        self._add_decl(name, node, "parameter")
            elif node_type in table.import_containers:
                for name in self._idents_below(node):
                    self._add_decl(name, node, "import")
                continue
            elif node_type in table.decls:
                hint = self._decl_kind_hint(node_type)
                for name in self._definition_names(node):
                    self._add_decl(name, node, hint)
            elif node_type in table.assigns:
                for name in self._write_targets(node):
                    self.assign_index.setdefault(name, []).append(node)
            stack.extend(node.children)
        for _, name, node in sorted(sink_hits, key=lambda hit: hit[0]):
            self.sink_nodes.setdefault(name, node)

    def _decl_kind_hint(self, node_type: str) -> str | None:
        if node_type in self.table.param_decls:
            return "parameter"
        if node_type in self.table.import_decls:
            return "import"
        return None  # field/global/local decided by the declaration's scope

    def _add_decl(self, name: str, node, kind_hint: str | None) -> None:
        if name:
            self.decl_index.setdefault(name, []).append((node, kind_hint))

    def _idents_below(self, subtree) -> list[str]:
        names, stack = [], [subtree]
        while stack:
            node = stack.pop()
            if node.type in self.table.idents:
                names.append(self._text(node))
            stack.extend(node.children)
        return names

    def _definition_names(self, node) -> list[str]:
        """Identifiers on the definition side of a declaring node."""
        subtrees: list = []
        for field in _NAME_FIELDS:
            subtrees = node.children_by_field_name(field)
            if subtrees:
                break
        if not subtrees:
            subtrees = [node]
        names: list[str] = []
        excluded: set[int] = set()
        stack = list(subtrees)
        while stack:
            current = stack.pop()
            if current.id in excluded:
                continue
            if current.type in self.table.idents:
                names.append(self._text(current))
                continue
            for field in _VALUE_FIELDS:
                for child in current.children_by_field_name(field):
                    if current is not node or child not in subtrees:
                        excluded.add(child.id)
            stack.extend(current.children)
        return names

    # -- member-position roles ----------------------------------------------
    def _member_role(self, node) -> str:
        """plain | member_self | member_foreign | receiver (position of an identifier)."""
        parent = node.parent
        member_types = self.table.member_parent or {}
        rule = member_types.get(parent.type) if parent is not None else None
        if rule is None:
            return "plain"
        member_field, receiver_field = rule
        member = parent.child_by_field_name(member_field)
        receiver = parent.child_by_field_name(receiver_field)
        if member is not None and member.id == node.id:
            if receiver is None:
                return "plain"  # receiver-less call: implicit this / file-local
            return (
                "member_self"
                if self._text(receiver) in self.table.self_names
                else "member_foreign"
            )
        if receiver is not None and receiver.id == node.id:
            return "receiver"
        return "plain"

    def _readable(self, node) -> bool:
        """A read of this identifier feeds the sink (member names need self/this)."""
        return self._member_role(node) != "member_foreign"

    def _write_targets(self, assign_node) -> list[str]:
        """Names actually (re)bound by an assignment.

        A member write (``doc.location.hash = …``) does not rebind the base
        identifier, so receiver-position names are excluded here even though
        they are traced on reads; ``this/self`` member writes count as field
        writes of the member name.
        """
        targets: list[str] = []
        for lhs in assign_node.children_by_field_name("left"):
            stack = [lhs]
            while stack:
                current = stack.pop()
                if current.type in self.table.idents:
                    if self._member_role(current) in ("plain", "member_self"):
                        targets.append(self._text(current))
                    continue
                stack.extend(current.children)
        return targets

    # -- scopes ---------------------------------------------------------------
    def _innermost_scope(self, node):
        current = node.parent if node is not None else None
        while (
            current is not None
            and current.type not in self.table.scopes
            and current.parent is not None
        ):
            current = current.parent
        return current if current is not None else self.tree.root_node

    def _scope_chain(self, node) -> list:
        chain, current = [], self._innermost_scope(node)
        while current is not None:
            chain.append(current)
            if current.parent is None:
                break
            current = self._innermost_scope(current)
        return chain

    def enclosing_chain(self) -> tuple[ScopeRef, ...]:
        row = self.sink_line - 1
        column = max(0, len(self.code_lines[row]) - 1)
        node = self.tree.root_node.descendant_for_point_range((row, 0), (row, column))
        refs = [
            ScopeRef(
                kind=scope.type,
                line=self._line(scope),
                text=self._snippet(self._line(scope)),
                name=self._scope_name(scope),
            )
            for scope in self._scope_chain(node)
            if scope.parent is not None and scope.type in self.table.scopes
        ]
        return tuple(refs)

    def _scope_name(self, scope) -> str:
        field = (self.table.scope_names or {}).get(scope.type)
        if field is None:
            return ""
        for child in scope.children_by_field_name(field):
            names = self._definition_names(child)
            if names:
                return names[0]
        return ""

    def find_occurrence(self, name: str, near_line: int):
        """The identifier node with this exact text closest to ``near_line``."""
        best, best_distance = None, None
        stack = [self.tree.root_node]
        while stack:
            node = stack.pop()
            if node.type in self.table.idents and self._text(node) == name:
                distance = abs(self._line(node) - near_line)
                if best_distance is None or distance < best_distance:
                    best, best_distance = node, distance
            stack.extend(node.children)
        return best

    # -- resolution ---------------------------------------------------------
    def _classify_decl(self, node, kind_hint: str | None) -> str:
        if kind_hint is not None:
            return kind_hint
        scope = self._innermost_scope(node)
        if scope.type in _CLASS_LIKE_SCOPES:
            return "field"
        if scope.parent is None or scope.type in _ROOT_SCOPES:
            return "global"
        return "local"

    def _resolve(self, name: str, occurrence) -> tuple[
        list[tuple[object, str | None]], list[object], list[object]
    ]:
        candidates = self.decl_index.get(name, [])
        chain = self._scope_chain(occurrence)
        resolved: list[tuple[object, str | None]] = []
        decl_scope = None
        for scope in chain:
            level = [
                (node, hint)
                for node, hint in candidates
                if self._innermost_scope(node).id == scope.id
                or (node.type in self.table.scopes and node.id == scope.id)
            ]
            if level:
                resolved, decl_scope = level, scope
                break
        innermost_id = chain[0].id if chain else None
        assigns = self.assign_index.get(name, [])
        writes = [node for node in assigns if self._innermost_scope(node).id == innermost_id]
        cross_writes: list[object] = []
        decl_is_outer = decl_scope is not None and (
            decl_scope.type in _CLASS_LIKE_SCOPES
            or decl_scope.type in _ROOT_SCOPES
            or decl_scope.parent is None
        )
        if not resolved or decl_is_outer:
            cross_writes = [
                node for node in assigns if self._innermost_scope(node).id != innermost_id
            ]
        return resolved, writes, cross_writes

    def facts(self) -> tuple[IdentifierFact, ...]:
        collected: dict[str, IdentifierFact] = {}
        queue: list[tuple[str, object]] = list(self.sink_nodes.items())
        seen: set[str] = set(self.sink_nodes)
        while queue and len(collected) < SLICE_MAX_FACTS:
            name, occurrence = queue.pop(0)
            decls, writes, cross_writes = self._resolve(name, occurrence)
            collected[name] = IdentifierFact(
                name=name,
                decls=self._decl_refs(decls),
                writes=self._write_refs(writes, "write"),
                cross_writes=self._write_refs(cross_writes, "cross_write"),
            )
            # two decl nodes: C/C++ register both `declaration` (no RHS field)
            # and the inner `init_declarator` (carries the RHS)
            for source_node, _ in decls[:2]:
                self._expand_rhs(source_node, seen, queue)
            for source_node in writes[:3] + cross_writes[:3]:
                self._expand_rhs(source_node, seen, queue)
        return tuple(collected.values())

    def _expand_rhs(self, node, seen: set[str], queue: list) -> None:
        for field in _RHS_FIELDS:
            for rhs in node.children_by_field_name(field):
                stack = [rhs]
                while stack:
                    current = stack.pop()
                    if current.type in self.table.idents:
                        name = self._text(current)
                        if name and name not in seen and self._readable(current):
                            seen.add(name)
                            queue.append((name, current))
                        continue
                    stack.extend(current.children)

    def _decl_refs(self, decls: list[tuple[object, str | None]]) -> tuple[SourceRef, ...]:
        refs = [
            SourceRef(
                line=self._line(node),
                kind=self._classify_decl(node, hint),
                text=self._snippet(self._line(node)),
            )
            for node, hint in decls
        ]
        return self._dedupe(refs)

    def _write_refs(self, nodes: list, kind: str) -> tuple[SourceRef, ...]:
        refs = [
            SourceRef(line=self._line(node), kind=kind, text=self._snippet(self._line(node)))
            for node in nodes
        ]
        return self._dedupe(refs)

    @staticmethod
    def _dedupe(refs: list[SourceRef]) -> tuple[SourceRef, ...]:
        unique: dict[tuple[int, str], SourceRef] = {}
        for ref in refs:
            unique.setdefault((ref.line, ref.kind), ref)
        return tuple(sorted(unique.values(), key=lambda ref: ref.line))
