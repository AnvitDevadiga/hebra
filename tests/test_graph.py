from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from conftest import fs_index
from hebra.graph.build import assign_symbol_ids, is_test_symbol, module_names
from hebra.graph.traverse import neighbors

PKG = {
    "src/shop/__init__.py": "from .pricing import total as total_price\n",
    "src/shop/pricing.py": (
        "from . import tax\n"
        "from .tax import rate as tax_rate\n"
        "import shop.discounts as disc\n"
        "\n"
        "def subtotal(items):\n"
        "    return sum(items)\n"
        "\n"
        "def total(items):\n"
        "    base = subtotal(items)\n"
        "    return base + tax.apply(base) * tax_rate() - disc.discount(base)\n"
    ),
    "src/shop/tax.py": (
        "from shop import pricing  # cyclic import\n"
        "\n"
        "def rate():\n"
        "    return 0.2\n"
        "\n"
        "def apply(amount):\n"
        "    return amount * rate()\n"
    ),
    "src/shop/discounts.py": (
        "import missing_vendor_lib\n"
        "from .nowhere import ghost\n"
        "\n"
        "def discount(amount):\n"
        "    return missing_vendor_lib.compute(amount) + ghost()\n"
    ),
    "src/shop/cart.py": (
        "import importlib\n"
        "from shop import total_price\n"
        "\n"
        "class Cart:\n"
        "    TAX = 1\n"
        "\n"
        "    def __init__(self):\n"
        "        self.items = []\n"
        "\n"
        "    def add(self, item):\n"
        "        self.items.append(item)\n"
        "        return self.price()\n"
        "\n"
        "    def price(self):\n"
        "        return total_price(self.items)\n"
        "\n"
        "    def checkout(self, gateway):\n"
        "        gateway.charge(self.price())\n"
        "        handler = getattr(self, 'add')\n"
        "        mod = importlib.import_module('shop.tax')\n"
        "        return self.refund()\n"
        "\n"
        "def outer():\n"
        "    def helper():\n"
        "        return 1\n"
        "    def caller():\n"
        "        return helper()\n"
        "    return caller()\n"
        "\n"
        "def charge(x):\n"
        "    return x\n"
        "\n"
        "def helper():\n"
        "    return 2\n"
    ),
    "src/shop/gateway.py": "class Gateway:\n    def charge(self, amount):\n        return amount\n",
    "src/shop/dupes.py": (
        "import sys\n"
        "if sys.platform == 'win32':\n"
        "    def f():\n"
        "        return 1\n"
        "else:\n"
        "    def f():\n"
        "        return 2\n"
        "def g():\n"
        "    return f()\n"
    ),
    "tests/test_cart.py": (
        "from shop.cart import Cart\n\ndef test_add():\n    assert Cart().add(1) == 1\n"
    ),
}


@pytest.fixture
def graph_repo(make_repo) -> Path:
    root = make_repo(PKG)
    fs_index(root)
    return root


def edges(root: Path, **where: str) -> set[tuple[str, ...]]:
    conn = sqlite3.connect(root / ".hebra" / "index.sqlite3")
    try:
        clause = " AND ".join(f"{k} = ?" for k in where) or "1"
        rows = conn.execute(
            f"SELECT src, dst, kind, status FROM edges WHERE {clause}", tuple(where.values())
        ).fetchall()
        return {tuple("" if v is None else v for v in row) for row in rows}
    finally:
        conn.close()


def resolved_targets(root: Path, src: str, kind: str = "calls") -> set[str]:
    return {dst for s, dst, k, st in edges(root, src=src) if k == kind and st == "resolved"}


P = "src/shop/pricing.py::"
T = "src/shop/tax.py::"
C = "src/shop/cart.py::"


def test_module_names_and_ids() -> None:
    assert module_names("src/shop/tax.py") == ["src.shop.tax", "shop.tax"]
    assert module_names("pkg/__init__.py") == ["pkg"]
    assert module_names("docs/readme.md") == []
    assert module_names("my-scripts/run.py") == []  # not importable
    assert assign_symbol_ids("a.py", ["f", "g", "f"]) == ["a.py::f", "a.py::g", "a.py::f#2"]
    assert is_test_symbol("tests/test_x.py", "test_a", "function")
    assert is_test_symbol("tests/test_x.py", "TestX.test_a", "method")
    assert not is_test_symbol("src/x.py", "test_a", "function")


def test_cross_module_relative_and_alias_imports(graph_repo: Path) -> None:
    # `tax.apply` via `from . import tax`, `tax_rate` via alias, `disc.discount`
    # via `import shop.discounts as disc`, `subtotal` in the same module.
    assert resolved_targets(graph_repo, P + "total") == {
        P + "subtotal",
        T + "apply",
        T + "rate",
        "src/shop/discounts.py::discount",
    }


def test_reexport_through_package_init(graph_repo: Path) -> None:
    assert resolved_targets(graph_repo, C + "Cart.price") == {P + "total"}


def test_self_method_calls_and_missing_attribute(graph_repo: Path) -> None:
    assert resolved_targets(graph_repo, C + "Cart.add") == {C + "Cart.price"}
    out = edges(graph_repo, src=C + "Cart.checkout")
    # self.refund is not defined on Cart: never an invented edge
    assert "unresolved" in {st for _, dst, _, st in out if dst == ""}
    assert all(dst != C + "Cart.refund" for _, dst, _, _ in out)


def test_dynamic_dispatch_is_never_resolved(graph_repo: Path) -> None:
    out = edges(graph_repo, src=C + "Cart.checkout")
    statuses = {st for _, _, k, st in out if k == "calls"}
    assert "dynamic" in statuses  # getattr / importlib.import_module
    assert "uncertain" in statuses  # gateway.charge: receiver type unknown
    # Neither the module-level `charge` nor Gateway.charge is claimed as callee.
    targets = {dst for _, dst, _, st in out if st == "resolved"}
    assert C + "charge" not in targets
    assert "src/shop/gateway.py::Gateway.charge" not in targets


def test_nested_functions_shadow_module_level(graph_repo: Path) -> None:
    caller = C + "outer.<locals>.caller"
    assert resolved_targets(graph_repo, caller) == {C + "outer.<locals>.helper"}
    assert (C + "outer", C + "outer.<locals>.helper", "contains", "resolved") in edges(graph_repo)


def test_same_name_definitions_get_distinct_ids(graph_repo: Path) -> None:
    conn = sqlite3.connect(graph_repo / ".hebra" / "index.sqlite3")
    ids = [
        r[0]
        for r in conn.execute(
            "SELECT symbol_id FROM symbols WHERE symbol_id LIKE 'src/shop/dupes.py::f%' "
            "ORDER BY start_line"
        )
    ]
    conn.close()
    assert ids == ["src/shop/dupes.py::f", "src/shop/dupes.py::f#2"]
    # g's call resolves to the first definition (documented approximation)
    assert resolved_targets(graph_repo, "src/shop/dupes.py::g") == {"src/shop/dupes.py::f"}


def test_cyclic_imports_are_plain_edges(graph_repo: Path) -> None:
    imports = edges(graph_repo, kind="imports", status="resolved")
    assert ("file:src/shop/pricing.py", "file:src/shop/tax.py", "imports", "resolved") in imports
    assert ("file:src/shop/tax.py", "file:src/shop/pricing.py", "imports", "resolved") in imports
    found = neighbors(
        sqlite3.connect(graph_repo / ".hebra" / "index.sqlite3"), [P + "total"], max_hops=2
    )
    assert len({n.node for n in found}) == len(found)  # no node visited twice


def test_missing_and_external_modules_are_unresolved(graph_repo: Path) -> None:
    out = edges(graph_repo, src="file:src/shop/discounts.py", kind="imports")
    assert {st for *_, st in out} == {"unresolved"}
    calls = edges(graph_repo, src="src/shop/discounts.py::discount")
    assert {st for _, _, k, st in calls if k == "calls"} == {"unresolved"}


def test_test_functions_link_to_source(graph_repo: Path) -> None:
    tests = edges(graph_repo, kind="tests")
    assert tests == {("tests/test_cart.py::test_add", C + "Cart", "tests", "resolved")}
    # `Cart().add(1)` needs type inference of a call result: recorded as dynamic,
    # not guessed as Cart.add.
    calls = edges(graph_repo, src="tests/test_cart.py::test_add", kind="calls")
    assert ("tests/test_cart.py::test_add", "", "calls", "dynamic") in calls


def test_class_body_names_are_not_visible_in_methods(make_repo) -> None:
    root = make_repo(
        {
            "m.py": (
                "def TAX():\n    return 1\n"
                "class A:\n"
                "    def TAX(self):\n        return 2\n"
                "    def use(self):\n        return TAX()\n"
            )
        }
    )
    fs_index(root)
    assert resolved_targets(root, "m.py::A.use") == {"m.py::TAX"}


def test_deleted_and_renamed_files_leave_no_dangling_edges(graph_repo: Path) -> None:
    (graph_repo / "src/shop/tax.py").rename(graph_repo / "src/shop/levy.py")
    (graph_repo / "src/shop/discounts.py").unlink()
    fs_index(graph_repo)
    conn = sqlite3.connect(graph_repo / ".hebra" / "index.sqlite3")
    symbol_ids = {r[0] for r in conn.execute("SELECT symbol_id FROM symbols")}
    files = {f"file:{r[0]}" for r in conn.execute("SELECT path FROM files")}
    dangling = [
        r
        for r in conn.execute("SELECT dst FROM edges WHERE dst IS NOT NULL")
        if r[0] not in symbol_ids | files
    ]
    conn.close()
    assert dangling == []
    # pricing's references to the old module are now honestly unresolved
    assert resolved_targets(graph_repo, P + "total") == {P + "subtotal"}


def test_traversal_is_bounded_and_deterministic(graph_repo: Path) -> None:
    conn = sqlite3.connect(graph_repo / ".hebra" / "index.sqlite3")
    one = neighbors(conn, [P + "total"], max_hops=1)
    assert {n.node for n in one} >= {P + "subtotal", T + "apply", T + "rate"}
    assert all(n.hops == 1 for n in one)
    capped = neighbors(conn, [P + "total"], max_hops=2, max_nodes=3)
    assert len(capped) == 3
    assert capped == neighbors(conn, [P + "total"], max_hops=2, max_nodes=3)
    with pytest.raises(ValueError):
        neighbors(conn, [P + "total"], max_hops=3)
    conn.close()


def test_unparseable_python_contributes_no_edges(make_repo) -> None:
    root = make_repo({"bad.py": "def broken(:\n    pass\n", "ok.py": "def a():\n    return b()\n"})
    fs_index(root)
    assert all(not src.startswith("bad.py") for src, *_ in edges(root))
