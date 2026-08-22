import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_framework_neutral_and_executor_layers_do_not_import_application_layers() -> None:
    for package in ("shared", "executor"):
        for path in (ROOT / package).rglob("*.py"):
            imports = _imports(path)
            assert not any(name == "src" or name.startswith("src.") for name in imports), path
            if package == "shared":
                assert not any(name == "server" or name.startswith("server.") for name in imports), path
