#!/usr/bin/env python3
"""Dependency-free test runner.

The suite is written for pytest and `pytest -q` is the normal way to run it.
This script exists so the tests also run in environments without pytest
installed: it injects a minimal stub providing `approx`, `raises` and `mark`,
then discovers and runs every `test_*` function in tests/.
"""

from __future__ import annotations

import importlib
import sys
import traceback
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def _install_pytest_stub() -> None:
    try:
        import pytest  # noqa: F401
        return
    except ImportError:
        pass

    stub = types.ModuleType("pytest")

    class Approx:
        def __init__(self, expected, rel=None, abs=None):
            self.expected = expected
            self.rel = rel
            self.abs = abs

        def _close(self, a, b) -> bool:
            if self.abs is not None and abs(a - b) <= self.abs:
                return True
            rel = self.rel if self.rel is not None else 1e-6
            return abs(a - b) <= rel * max(abs(a), abs(b), 1e-12)

        def __eq__(self, other):
            if isinstance(self.expected, (list, tuple)):
                return len(other) == len(self.expected) and all(
                    self._close(x, y) for x, y in zip(other, self.expected)
                )
            return self._close(other, self.expected)

        def __repr__(self):
            return f"approx({self.expected!r})"

    class RaisesContext:
        def __init__(self, exc):
            self.exc = exc

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            if exc_type is None:
                raise AssertionError(f"DID NOT RAISE {self.exc}")
            return issubclass(exc_type, self.exc)

    stub.approx = lambda expected, rel=None, abs=None: Approx(expected, rel, abs)
    stub.raises = lambda exc: RaisesContext(exc)
    stub.fail = lambda msg="": (_ for _ in ()).throw(AssertionError(msg))

    class _Mark:
        def __getattr__(self, _name):
            return lambda *a, **k: (lambda f: f)

    stub.mark = _Mark()
    sys.modules["pytest"] = stub


def main() -> int:
    _install_pytest_stub()

    modules = sorted(p.stem for p in (ROOT / "tests").glob("test_*.py"))
    passed = failed = 0
    failures: list[tuple[str, str]] = []

    for mod_name in modules:
        module = importlib.import_module(f"tests.{mod_name}")
        for name in sorted(dir(module)):
            if not name.startswith("test_"):
                continue
            fn = getattr(module, name)
            if not callable(fn):
                continue
            try:
                fn()
            except Exception:
                failed += 1
                failures.append((f"{mod_name}::{name}", traceback.format_exc()))
                print("F", end="", flush=True)
            else:
                passed += 1
                print(".", end="", flush=True)

    print()
    for name, tb in failures:
        print(f"\n=== FAILED {name} ===\n{tb}")

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
