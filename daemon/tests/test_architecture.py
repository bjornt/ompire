"""Executable architecture rules for the work/launch boundary.

Static import checks over the daemon source, using the standard `ast` module
only: module-level, function-local, and `TYPE_CHECKING` imports are all
inspected, absolute and relative forms are normalized, `from package import
submodule` is followed, and literal dynamic imports are resolved like any
other. Nothing here imports or executes daemon modules to analyze them.

The rules and their exceptions live in this file as checked-in tables — part
of the ordinary pytest run, not a generated baseline. An exception names the
importing module, the imported module and symbol, and the later epic change
that owns removing it; an exception that no longer matches a real import
fails the test so paid-off debt cannot linger as permission for its return.

What the checker cannot establish — SQL column ownership, runtime reflection,
security — remains code review's job. Behavioral tests cover transactions.
Frontend dependency enforcement is out of scope here.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parent.parent / "src"
PACKAGE_ROOT = SRC_ROOT / "ompire_daemon"
MODULE = "ompire_daemon"

# Roots whose Python sources are also scanned for imports of retired module
# paths (production ownership checks apply to the daemon package only; tests
# may inspect internals of the owner under test, but no root may reach a
# moved module's old path).
CALLSITE_ROOTS = [
    PACKAGE_ROOT,
    Path(__file__).resolve().parent,
    SRC_ROOT.parent.parent / "local-test",
    SRC_ROOT.parent.parent / "scripts",
]


# --------------------------------------------------------------------------
# Ownership: every production module must declare an owner via this map.
# A new module that is not classified fails the check until it is added.

OWNER_BY_PREFIX: dict[str, str] = {
    f"{MODULE}.platform": "platform",
    f"{MODULE}.model_config": "model-values",
    f"{MODULE}.work": "work",
    f"{MODULE}.oversight": "oversight",
    f"{MODULE}.application": "application",
    f"{MODULE}.api": "api",
    f"{MODULE}.registry": "registry",
    f"{MODULE}.workflows": "workflows",
    f"{MODULE}.workflow_definitions": "workflows",
    f"{MODULE}.builtin_workflows": "workflows",
    f"{MODULE}.taskdefinition": "workflows",
    f"{MODULE}.runauthority": "workflows",
    f"{MODULE}.agent": "sessions",
    f"{MODULE}.rpc": "sessions",
    f"{MODULE}.sessions": "sessions",
    f"{MODULE}.delivery": "delivery",
    f"{MODULE}.review": "delivery",
    f"{MODULE}.ship": "delivery",
    f"{MODULE}.gpg": "delivery",
    f"{MODULE}.gh": "delivery",
    f"{MODULE}.prwatch": "delivery",
    f"{MODULE}.results": "artifacts",
    f"{MODULE}.result_exports": "artifacts",
    f"{MODULE}.handoff": "artifacts",
    f"{MODULE}.isolation": "isolation",
    f"{MODULE}.events": "core",
    f"{MODULE}.notifications": "oversight-flat",
    f"{MODULE}.advisories": "oversight-flat",
    f"{MODULE}.app": "core",
    f"{MODULE}.config": "core",
    f"{MODULE}.auth": "core",
    f"{MODULE}.static": "core",
    f"{MODULE}.db": "core",
    f"{MODULE}.migrate": "core",
    f"{MODULE}.datadir": "core",
    f"{MODULE}.__main__": "core",
    f"{MODULE}.recovery": "core",
}


def owner_of(module: str) -> str | None:
    """Longest-prefix owner lookup; subpackages inherit their root's owner.

    The daemon package itself is core; a brand-new top-level module matches
    nothing and is unclassified until declared.
    """
    if module == MODULE:
        return "core"
    best: str | None = None
    for prefix in OWNER_BY_PREFIX:
        longer = module == prefix or module.startswith(prefix + ".")
        if longer and (best is None or len(prefix) > len(best)):
            best = prefix
    return OWNER_BY_PREFIX[best] if best else None


def _within(module: str, prefix: str) -> bool:
    """True only inside the package named by `prefix` — `workflows` is not
    `work`, and comparing dotted strings without this would say it is."""
    return module == prefix or module.startswith(prefix + ".")


# --------------------------------------------------------------------------
# Import extraction


@dataclass(frozen=True)
class ImportEdge:
    importer: str  # dotted module doing the importing
    imported: str  # dotted module being imported
    symbol: str | None  # symbol for from-imports; None for module imports
    line: int


def _resolve_relative(current: str, node: ast.ImportFrom) -> str | None:
    """Resolve a relative import against the importing module's package."""
    if node.level == 0:
        return node.module or ""
    parts = current.split(".")
    # level 1 refers to the importing module's own package
    base = parts[:-1] if len(parts) > 1 else parts
    drop = node.level - 1
    if drop:
        if drop > len(base):
            return None
        base = base[: len(base) - drop]
    package = ".".join(base)
    return f"{package}.{node.module}" if node.module else package


def collect_imports(source: str, module: str) -> list[ImportEdge]:
    """Every import the module performs, at any scope, as normalized edges.

    `ast.walk` reaches function-local and `TYPE_CHECKING` imports with no
    special casing, because the boundary must hold wherever the import
    executes. `from package import submodule` also yields a module edge for
    the joined path so a moved submodule cannot be reached through its old
    parent.
    """
    tree = ast.parse(source)
    edges: list[ImportEdge] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                edges.append(ImportEdge(module, alias.name, None, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            target = _resolve_relative(module, node)
            if target is None:
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                edges.append(ImportEdge(module, target, alias.name, node.lineno))
                if (
                    alias.name[0].islower()
                    and target.startswith(f"{MODULE}.")
                    and "." not in alias.name
                ):
                    edges.append(
                        ImportEdge(module, f"{target}.{alias.name}", None, node.lineno)
                    )
        elif isinstance(node, ast.Call):
            func = node.func
            callee = None
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                callee = f"{func.value.id}.{func.attr}"
            elif isinstance(func, ast.Name):
                callee = func.id
            if callee in ("importlib.import_module", "__import__"):
                arg = node.args[0] if node.args else None
                literal = arg.value if isinstance(arg, ast.Constant) else None
                edges.append(
                    ImportEdge(
                        module, f"<dynamic:{literal!r}>", None, node.lineno
                    )
                )
    return edges


def iter_production_modules() -> list[tuple[str, Path]]:
    out = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(PACKAGE_ROOT).with_suffix("")
        parts = rel.parts
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        out.append((".".join([MODULE, *parts]) if parts else MODULE, path))
    return out


# --------------------------------------------------------------------------
# Rules

WORK_MODULES_PREFIX = f"{MODULE}.work"
APPLICATION_PREFIX = f"{MODULE}.application"
OVERSIGHT_PREFIX = f"{MODULE}.oversight"
ISOLATION_PREFIX = f"{MODULE}.isolation"
PLATFORM_PREFIX = f"{MODULE}.platform"
# Purity applies to the whole technical foundation, not one module: every
# platform module (and the shared model vocabulary) stays product-free.
PLATFORM_VALUES_MODULES = (
    *(f"{MODULE}.platform", f"{MODULE}.platform.transactions",
      f"{MODULE}.platform.processes", f"{MODULE}.platform.git"),
    f"{MODULE}.model_config",
)
WORK_ROUTERS = (
    f"{MODULE}.api.projects",
    f"{MODULE}.api.profiles",
    f"{MODULE}.api.tasks",
)
TRANSPORT_PREFIXES = (f"{MODULE}.api",)

# What a work router may not reach: the retired monolith, raw schema, and
# every other owner's execution machinery. Work's own operations (work.*) and
# the application commands are the intended surface; registry.settings stays
# admissible as the layered-configuration read the create command consumes.
ROUTER_FORBIDDEN_TARGETS = frozenset(
    {
        f"{MODULE}.api.rest",
        f"{MODULE}.db",
        f"{MODULE}.workflows",
        f"{MODULE}.agent",
        f"{MODULE}.rpc",
        f"{MODULE}.sessions",
        f"{MODULE}.review",
        f"{MODULE}.ship",
        f"{MODULE}.delivery",
        f"{MODULE}.gpg",
        f"{MODULE}.gh",
        f"{MODULE}.prwatch",
        f"{MODULE}.results",
        f"{MODULE}.result_exports",
        f"{MODULE}.registry.workflows",
        f"{MODULE}.registry.results",
        f"{MODULE}.registry.result_exports",
        f"{MODULE}.registry.ships",
        f"{MODULE}.registry.reviews",
        f"{MODULE}.registry.sessions",
        f"{MODULE}.registry.workflow_library",
        f"{MODULE}.registry.workflow_definitions",
    }
)

PROTECTED_OWNERS = frozenset(
    {"platform", "model-values", "isolation", "work", "application", "oversight", "api"}
)

# The resource boundary's declared public surface. Cross-owner consumers
# import exactly these names, from the package — never an isolation submodule
# or a private symbol.
ISOLATION_PUBLIC_SURFACE = frozenset(
    {
        "ExcludeUpdateError",
        "GIT_EXCLUDE_OMPIRE_ENTRY",
        "PhaseProgress",
        "SandboxCommandError",
        "SandboxCommandResult",
        "WorkshopRemoveError",
        "WorkspaceBlockedError",
        "WorkspaceBusyError",
        "WorkspaceDeletionRefused",
        "WorkspaceGuard",
        "WorkspaceOperationError",
        "WorkspaceRef",
        "WorkspaceSpec",
        "destroy_workspace",
        "ensure_git_excludes",
        "exclude_pattern_for",
        "launch_workshop",
        "prepare_clone",
        "recover_pending",
        "remove_workshop",
        "run_sandbox_command",
        "start_sandbox_process",
        "verify_pinned_source",
        "workshop_status",
    }
)

# Checked-in migration exceptions: (importer, imported module, symbol) ->
# the later epic change that owns removing the edge. Each must match exactly
# one real import edge; a stale entry fails the test.
EXCEPTIONS: dict[tuple[str, str, str], str] = {
    (
        f"{MODULE}.registry.workflows",
        f"{MODULE}.work.tasks",
        "_row_to_task",
    ): "separate-workflow-semantics-and-effect-coordination",
    (
        f"{MODULE}.registry.workflows",
        f"{MODULE}.work.tasks",
        "_update",
    ): "separate-workflow-semantics-and-effect-coordination",
}

# Retired module paths: no source, test, tool, or harness root may import
# them, and the files must not exist (no compatibility re-exports).
RETIRED_MODULES = frozenset(
    {
        f"{MODULE}.registry.model_profiles",
        f"{MODULE}.registry.projects",
        f"{MODULE}.registry.tasks",
        f"{MODULE}.registry.launch",
        f"{MODULE}.launch",
        f"{MODULE}.execution_inputs",
        f"{MODULE}.launchconfig",
        f"{MODULE}.projectcheckout",
        f"{MODULE}.projectsetup",
        f"{MODULE}.projectfiles",
        f"{MODULE}.spawn",
        f"{MODULE}.workshop",
        f"{MODULE}.workshopadditions",
    }
)
RETIRED_FILES = {
    PACKAGE_ROOT / "launch.py",
    PACKAGE_ROOT / "execution_inputs.py",
    PACKAGE_ROOT / "launchconfig.py",
    PACKAGE_ROOT / "projectcheckout.py",
    PACKAGE_ROOT / "projectsetup.py",
    PACKAGE_ROOT / "projectfiles.py",
    PACKAGE_ROOT / "registry" / "model_profiles.py",
    PACKAGE_ROOT / "registry" / "projects.py",
    PACKAGE_ROOT / "registry" / "tasks.py",
    PACKAGE_ROOT / "registry" / "launch.py",
    PACKAGE_ROOT / "spawn.py",
    PACKAGE_ROOT / "workshop.py",
    PACKAGE_ROOT / "workshopadditions.py",
}

# Symbols whose canonical home moved off a module that still exists: importing
# them from the old owner is a retired path too, not a re-export.
RETIRED_QUALIFIED_SYMBOLS = {
    f"{MODULE}.delivery": frozenset(
        {
            "WorkspaceGuard",
            "WorkspaceBusyError",
            "WorkspaceBlockedError",
            "safe_git",
            "run_git",
            "git_out",
        }
    ),
}

# Direct submodule imports are for the package's own wiring; cross-owner
# consumers get the declared surface only.
ISOLATION_SUBMODULES = frozenset(
    f"{ISOLATION_PREFIX}.{name}"
    for name in ("additions", "excludes", "guard", "workshop", "workspace")
)


def check_tree(modules: list[tuple[str, Path]]) -> list[str]:
    """Every rule violation across the given (module, path) pairs."""
    violations: list[str] = []
    all_edges: list[ImportEdge] = []

    for module, path in modules:
        edges = collect_imports(path.read_text(), module)
        all_edges.extend(edges)
        owner = owner_of(module)
        if owner is None:
            violations.append(
                f"{module}: unclassified production module — declare its owner "
                "in OWNER_BY_PREFIX"
            )
            continue

        for edge in edges:
            target = edge.imported
            located = f"{path.name}:{edge.line}"

            # --- platform/value purity: stdlib (+ SQLAlchemy for the
            # transaction primitive) only ---------------------------------
            if module in PLATFORM_VALUES_MODULES:
                product = target.startswith(f"{MODULE}.") and (
                    owner_of(target) != "platform"
                )
                if product:
                    violations.append(
                        f"{located}: {module} imports product code {target} — "
                        "platform/value modules stay pure"
                    )
                elif target == "sqlalchemy" and module == f"{MODULE}.model_config":
                    violations.append(
                        f"{located}: model_config must not depend on SQLAlchemy"
                    )

            # --- isolation purity: the resource boundary stays resource-only -
            if _within(module, ISOLATION_PREFIX):
                if target.startswith(f"{MODULE}.") and owner_of(target) not in (
                    "isolation",
                    "platform",
                ):
                    violations.append(
                        f"{located}: isolation module {module} imports product "
                        f"code {target} — the resource boundary must stay "
                        "usable without any product owner"
                    )
                if target in ("fastapi", "starlette", "pydantic"):
                    violations.append(
                        f"{located}: isolation module {module} imports {target}"
                    )

            # --- isolation public surface -----------------------------------
            if owner != "isolation" and target.startswith(f"{MODULE}."):
                if target in ISOLATION_SUBMODULES:
                    violations.append(
                        f"{located}: {module} imports isolation submodule "
                        f"{target} directly — import the declared surface "
                        f"from {ISOLATION_PREFIX}"
                    )
                elif (
                    target == ISOLATION_PREFIX
                    and edge.symbol is not None
                    and edge.symbol not in ISOLATION_PUBLIC_SURFACE
                ):
                    violations.append(
                        f"{located}: {module} imports {target}.{edge.symbol} — "
                        "not part of the declared isolation public surface"
                    )

            # --- retired qualified symbols ----------------------------------
            retired_symbols = RETIRED_QUALIFIED_SYMBOLS.get(target)
            if retired_symbols and edge.symbol in retired_symbols:
                violations.append(
                    f"{located}: {target}.{edge.symbol} moved off {target} — "
                    "import it from its canonical owner"
                )

            # --- work persistence/values: no transport, commands, or
            # projection ---------------------------------------------------
            if _within(module, WORK_MODULES_PREFIX):
                for prefix in TRANSPORT_PREFIXES:
                    if _within(target, prefix):
                        violations.append(
                            f"{located}: work module {module} imports "
                            f"transport {target}"
                        )
                if target.startswith(APPLICATION_PREFIX):
                    violations.append(
                        f"{located}: work module {module} imports application "
                        f"command {target}"
                    )
                if target.startswith(OVERSIGHT_PREFIX):
                    violations.append(
                        f"{located}: work module {module} imports projection "
                        f"{target}"
                    )
                if target in ("fastapi", "starlette", "pydantic"):
                    violations.append(
                        f"{located}: work module {module} imports {target}"
                    )

            # --- application commands: no transport ------------------------
            if _within(module, APPLICATION_PREFIX):
                for prefix in TRANSPORT_PREFIXES:
                    if _within(target, prefix):
                        violations.append(
                            f"{located}: application module {module} imports "
                            f"transport {target}"
                        )
                if target in ("fastapi", "starlette"):
                    violations.append(
                        f"{located}: application module {module} imports {target}"
                    )

            # --- work routers: public commands/queries only -----------------
            if module in WORK_ROUTERS:
                hit = next(
                    (
                        forbidden
                        for forbidden in ROUTER_FORBIDDEN_TARGETS
                        if target == forbidden or target.startswith(forbidden + ".")
                    ),
                    None,
                )
                forbidden_hit = hit is not None
                if forbidden_hit:
                    violations.append(
                        f"{located}: work router imports {target} — call the "
                        "public commands/queries instead"
                    )
                if (
                    edge.symbol is not None
                    and edge.symbol.startswith("_")
                    and (
                        _within(target, f"{MODULE}.work")
                        or _within(target, f"{MODULE}.registry")
                        or _within(target, f"{MODULE}.oversight")
                        or _within(target, f"{MODULE}.application")
                    )
                ):
                    violations.append(
                        f"{located}: work router imports private symbol "
                        f"{target}.{edge.symbol}"
                    )

            # --- cross-owner private imports --------------------------------
            cross_owner_private = (
                edge.symbol is not None
                and edge.symbol.startswith("_")
                and target.startswith(f"{MODULE}.")
                and owner_of(target) not in (owner, None)
            )
            if cross_owner_private and (module, target, edge.symbol) not in EXCEPTIONS:
                violations.append(
                    f"{located}: cross-owner private import "
                    f"{target}.{edge.symbol} (owner {owner_of(target)}, "
                    f"importer owner {owner}) without a checked-in exception"
                )

            # --- dynamic imports in protected modules ------------------------
            if target.startswith("<dynamic:") and owner in PROTECTED_OWNERS:
                if "None" in target:
                    violations.append(
                        f"{located}: computed dynamic import in protected "
                        f"module {module} — static policy cannot follow it"
                    )
                else:
                    literal = target[len("<dynamic:") :].rstrip(">").strip("'\"")
                    if literal.startswith(f"{MODULE}."):
                        violations.append(
                            f"{located}: dynamic import of {literal} in "
                            f"protected module {module} — import it statically"
                        )

    return violations


def stale_exceptions(edges: list[ImportEdge]) -> list[str]:
    """Exceptions that no longer match a real import edge."""
    real = {(e.importer, e.imported, e.symbol) for e in edges}
    return [
        f"stale architecture exception {key} — the debt it admitted is gone; "
        "remove the entry"
        for key in EXCEPTIONS
        if key not in real
    ]


# --------------------------------------------------------------------------
# Tests


def test_retired_module_files_are_gone() -> None:
    present = [str(p) for p in RETIRED_FILES if p.exists()]
    assert not present, f"retired modules still present: {present}"


def test_no_retired_module_imports_anywhere() -> None:
    problems: list[str] = []
    for root in CALLSITE_ROOTS:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts or ".state" in path.parts:
                continue
            source = path.read_text()
            try:
                edges = collect_imports(source, path.stem)
            except SyntaxError:
                continue
            for edge in edges:
                if edge.imported in RETIRED_MODULES:
                    problems.append(
                        f"{path}:{edge.line}: imports retired module "
                        f"{edge.imported}"
                    )
                retired = RETIRED_QUALIFIED_SYMBOLS.get(edge.imported)
                if retired and edge.symbol in retired:
                    problems.append(
                        f"{path}:{edge.line}: imports {edge.imported}."
                        f"{edge.symbol} from its retired location — use the "
                        "canonical owner"
                    )
    assert not problems, "\n".join(problems)


def test_architecture_rules_hold() -> None:
    modules = iter_production_modules()
    assert modules, "production source tree not found"
    violations = check_tree(modules)
    assert not violations, "\n".join(violations)


def test_architecture_exceptions_stay_fresh() -> None:
    edges: list[ImportEdge] = []
    for module, path in iter_production_modules():
        edges.extend(collect_imports(path.read_text(), module))
    assert not stale_exceptions(edges), "\n".join(stale_exceptions(edges))


def test_every_production_module_is_classified() -> None:
    missing = [
        module for module, _ in iter_production_modules() if owner_of(module) is None
    ]
    assert not missing, f"unclassified modules: {missing}"


# --------------------------------------------------------------------------
# Checker self-tests: synthetic fixtures proving the policy catches what it
# claims to catch and allows what it must allow. They exercise dependency
# policy on synthetic trees, not incidental file contents.


def _synthetic(tmp_path: Path, module: str, source: str) -> tuple[str, Path]:
    rel = module.replace(".", "/")
    path = tmp_path / f"{rel}.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)
    return module, path


def test_checker_allows_legal_intra_owner_import(tmp_path: Path) -> None:
    modules = [
        _synthetic(
            tmp_path,
            f"{MODULE}.work.tasks",
            "from ompire_daemon.work.inputs import TaskExecutionInputs\n",
        )
    ]
    assert check_tree(modules) == []


def test_checker_catches_local_transport_import(tmp_path: Path) -> None:
    modules = [
        _synthetic(
            tmp_path,
            f"{MODULE}.work.tasks",
            "def f():\n    from ompire_daemon.api import rest\n",
        )
    ]
    violations = check_tree(modules)
    assert any("imports transport" in v for v in violations)


def test_checker_catches_type_checking_projection_import(tmp_path: Path) -> None:
    modules = [
        _synthetic(
            tmp_path,
            f"{MODULE}.work.tasks",
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            f"    from {MODULE}.oversight.tasks import task_payload\n",
        )
    ]
    violations = check_tree(modules)
    assert any("imports projection" in v for v in violations)


def test_checker_catches_relative_router_import_of_monolith(tmp_path: Path) -> None:
    modules = [
        _synthetic(
            tmp_path,
            f"{MODULE}.api.tasks",
            "from .rest import router\n",
        )
    ]
    violations = check_tree(modules)
    assert any("work router imports" in v for v in violations)


def test_checker_catches_private_symbol_from_router(tmp_path: Path) -> None:
    modules = [
        _synthetic(
            tmp_path,
            f"{MODULE}.api.profiles",
            "from ompire_daemon.work.tasks import _update\n",
        )
    ]
    violations = check_tree(modules)
    assert any("private symbol" in v for v in violations)


def test_checker_catches_transport_through_new_api_module(tmp_path: Path) -> None:
    # A fresh api-local module is still transport: work code reaching it
    # violates the boundary no matter how small the module is.
    modules = [
        _synthetic(
            tmp_path,
            f"{MODULE}.work.profiles",
            "from ompire_daemon.api.work_models import TaskOut\n",
        )
    ]
    violations = check_tree(modules)
    assert any("imports transport" in v for v in violations)


def test_checker_detects_obsolete_exception() -> None:
    edges = collect_imports(
        "from ompire_daemon.work.tasks import Task\n",
        f"{MODULE}.registry.workflows",
    )
    assert stale_exceptions(edges)


def test_checker_exception_admits_declared_edge(tmp_path: Path) -> None:
    modules = [
        _synthetic(
            tmp_path,
            f"{MODULE}.registry.workflows",
            "from ompire_daemon.work.tasks import _update\n"
            "from ompire_daemon.work.tasks import _row_to_task\n",
        )
    ]
    assert check_tree(modules) == []


def test_checker_rejects_computed_dynamic_import(tmp_path: Path) -> None:
    modules = [
        _synthetic(
            tmp_path,
            f"{MODULE}.platform.transactions",
            "import importlib\n"
            "name = 'os'\n"
            "mod = importlib.import_module(name)\n",
        )
    ]
    violations = check_tree(modules)
    assert any("computed dynamic import" in v for v in violations)


def test_checker_rejects_literal_dynamic_product_import(tmp_path: Path) -> None:
    modules = [
        _synthetic(
            tmp_path,
            f"{MODULE}.work.tasks",
            "import importlib\n"
            "mod = importlib.import_module('ompire_daemon.api.rest')\n",
        )
    ]
    violations = check_tree(modules)
    assert any("dynamic import of" in v for v in violations)


def test_checker_rejects_product_import_in_platform(tmp_path: Path) -> None:
    modules = [
        _synthetic(
            tmp_path,
            f"{MODULE}.platform.transactions",
            f"from {MODULE}.db import tasks\n",
        )
    ]
    violations = check_tree(modules)
    assert any("platform/value modules stay pure" in v for v in violations)


def test_checker_flags_unclassified_module(tmp_path: Path) -> None:
    modules = [
        _synthetic(
            tmp_path,
            f"{MODULE}.brandnew.unknown",
            "import os\n",
        )
    ]
    violations = check_tree(modules)
    assert any("unclassified production module" in v for v in violations)


def test_collect_imports_normalizes_from_package_import_submodule() -> None:
    edges = collect_imports(
        f"from {MODULE}.registry import tasks\n", f"{MODULE}.api.rest"
    )
    targets = {(e.imported, e.symbol) for e in edges}
    assert (f"{MODULE}.registry", "tasks") in targets
    # the joined module edge is what the retired-name scan relies on
    assert (f"{MODULE}.registry.tasks", None) in targets


def test_collect_imports_resolves_relative_forms() -> None:
    edges = collect_imports(
        "from .rest import router\nfrom ..work.tasks import Task\n",
        f"{MODULE}.api.tasks",
    )
    imported = {e.imported for e in edges}
    assert f"{MODULE}.api.rest" in imported
    assert f"{MODULE}.work.tasks" in imported


def test_checker_rejects_product_import_in_any_platform_module(tmp_path: Path) -> None:
    # Purity is a property of the whole technical foundation, not of the one
    # module that happened to exist first.
    for platform_module in ("platform.git", "platform.processes"):
        modules = [
            _synthetic(
                tmp_path,
                f"{MODULE}.{platform_module}",
                f"from {MODULE}.work.tasks import Task\n",
            )
        ]
        violations = check_tree(modules)
        assert any("platform/value modules stay pure" in v for v in violations)


def test_checker_rejects_product_import_in_isolation(tmp_path: Path) -> None:
    # The resource boundary must be exercisable with no product owner loaded:
    # a workflow, delivery, or transport import back into it is the coupling
    # this change exists to remove.
    for product in ("workflows", "delivery", "work.tasks", "api.rest", "rpc"):
        modules = [
            _synthetic(
                tmp_path,
                f"{MODULE}.isolation.workspace",
                f"from {MODULE}.{product} import Something\n",
            )
        ]
        violations = check_tree(modules)
        assert any("resource boundary must stay usable" in v for v in violations)


def test_checker_rejects_isolation_submodule_import_across_owners(tmp_path: Path) -> None:
    modules = [
        _synthetic(
            tmp_path,
            f"{MODULE}.ship",
            f"from {MODULE}.isolation.guard import WorkspaceGuard\n",
        )
    ]
    violations = check_tree(modules)
    assert any("imports isolation submodule" in v for v in violations)


def test_checker_rejects_undeclared_isolation_surface_symbol(tmp_path: Path) -> None:
    modules = [
        _synthetic(
            tmp_path,
            f"{MODULE}.ship",
            f"from {MODULE}.isolation import _staging_dir\n",
        )
    ]
    violations = check_tree(modules)
    assert any("not part of the declared isolation public surface" in v for v in violations)


def test_checker_rejects_retired_qualified_guard_import(tmp_path: Path) -> None:
    # The guard's canonical home moved to isolation; delivery must not grow a
    # re-export, and no caller may keep importing it from the old owner.
    modules = [
        _synthetic(
            tmp_path,
            f"{MODULE}.review",
            f"from {MODULE}.delivery import WorkspaceGuard\n",
        )
    ]
    violations = check_tree(modules)
    assert any("moved off" in v for v in violations)


def test_checker_allows_public_isolation_import_across_owners(tmp_path: Path) -> None:
    modules = [
        _synthetic(
            tmp_path,
            f"{MODULE}.ship",
            f"from {MODULE}.isolation import WorkspaceGuard\n",
        )
    ]
    assert check_tree(modules) == []


def test_checker_allows_intra_isolation_submodule_import(tmp_path: Path) -> None:
    modules = [
        _synthetic(
            tmp_path,
            f"{MODULE}.isolation.workspace",
            f"from {MODULE}.isolation.additions import stage\n",
        )
    ]
    assert check_tree(modules) == []
