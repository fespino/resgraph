"""Four structural assertions that make schema drift impossible, not
just currently absent. AST, not grep — survives formatting and aliased
imports."""

import ast
import inspect
from pathlib import Path

from pydantic import BaseModel

from resgraph.tools import registry as registry_module
from resgraph.tools.context import CallerContext
from resgraph.tools.registry import TOOL_REGISTRY

SRC = Path(__file__).parents[1] / "src" / "resgraph"
MCP_SERVER = SRC / "mcp" / "server.py"
HTTP_MOUNT = SRC / "tools" / "http.py"


def _py_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _calls(tree: ast.AST) -> list[ast.Call]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)]


def _attr_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def test_no_tool_registration_outside_the_registry_loop():
    offenders: list[str] = []
    for path in _py_files():
        if path == MCP_SERVER:
            continue
        tree = ast.parse(path.read_text())
        for call in _calls(tree):
            if _attr_name(call.func) == "add_tool":
                offenders.append(f"{path}:{call.lineno}")
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                for dec in node.decorator_list:
                    target = dec.func if isinstance(dec, ast.Call) else dec
                    if isinstance(target, ast.Attribute) and target.attr == "tool":
                        offenders.append(f"{path}:{node.lineno}")
    assert not offenders, f"tool registration outside {MCP_SERVER.name}: {offenders}"


def test_no_tools_route_outside_the_registry_mount():
    offenders: list[str] = []
    for path in _py_files():
        if path == HTTP_MOUNT:
            continue
        tree = ast.parse(path.read_text())
        for call in _calls(tree):
            name = _attr_name(call.func)
            args = call.args
            route_call = name in {"get", "post", "put", "delete", "patch", "add_api_route"}
            if not route_call or not args:
                continue
            first = args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                if first.value.startswith("/tools"):
                    offenders.append(f"{path}:{call.lineno}")
            elif isinstance(first, ast.JoinedStr):
                head = first.values[0]
                if isinstance(head, ast.Constant) and str(head.value).startswith("/tools"):
                    offenders.append(f"{path}:{call.lineno}")
    assert not offenders, f"/tools route outside {HTTP_MOUNT.name}: {offenders}"


def _class_names(root: Path) -> dict[str, str]:
    names: dict[str, str] = {}
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                names[node.name] = f"{path}:{node.lineno}"
    return names


def test_no_duplicated_model_across_surfaces():
    canonical = _class_names(SRC / "tools")
    offenders = [
        f"{where} shadows canonical {canonical[name]}"
        for surface in (SRC / "mcp", SRC / "api")
        for name, where in _class_names(surface).items()
        if name in canonical
    ]
    assert not offenders, f"one schema, one home: {offenders}"


def test_registry_and_implementations_agree():
    for entry in TOOL_REGISTRY:
        assert callable(entry.fn), entry.name
        sig = inspect.signature(entry.fn)
        params = list(sig.parameters.values())
        assert [p.name for p in params] == ["args", "ctx"], entry.name
        assert params[0].annotation is entry.input_model, entry.name
        assert params[1].kind is inspect.Parameter.KEYWORD_ONLY, entry.name
        assert params[1].annotation is CallerContext, entry.name
        assert sig.return_annotation is entry.output_model, entry.name
        assert issubclass(entry.input_model, BaseModel), entry.name
        assert issubclass(entry.output_model, BaseModel), entry.name

    registered = {entry.fn for entry in TOOL_REGISTRY}
    canonical_dir = SRC / "tools" / "canonical"
    orphans: list[str] = []
    for path in sorted(canonical_dir.glob("*.py")):
        module_name = f"resgraph.tools.canonical.{path.stem}"
        module = __import__(module_name, fromlist=["*"])
        for name, obj in vars(module).items():
            if (
                inspect.isfunction(obj)
                and not name.startswith("_")
                and obj.__module__ == module_name
                and obj not in registered
            ):
                orphans.append(f"{module_name}.{name}")
    assert not orphans, f"canonical functions missing from the registry: {orphans}"


def test_registry_module_is_the_only_registry():
    entries = [
        name
        for name, obj in vars(registry_module).items()
        if isinstance(obj, tuple) and name == "TOOL_REGISTRY"
    ]
    assert entries == ["TOOL_REGISTRY"]
