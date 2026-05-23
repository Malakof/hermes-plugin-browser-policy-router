"""Root conftest: keep pytest from collecting the plugin's own ``__init__.py``.

When pytest 8+ runs with ``--import-mode=importlib`` from a directory that
contains both a Python package (``__init__.py``) and a ``tests/`` subdir,
it tries to walk up from each test file to determine its parent package
and ends up trying to import the plugin's ``__init__.py`` as a free-
standing module — which fails because the file uses relative imports
that only work via the importlib spec the Hermes loader (and our test
conftest) uses.

This file pins the collection root here and explicitly excludes the
plugin's package init from discovery.
"""

from __future__ import annotations

import sys
from pathlib import Path


def pytest_ignore_collect(collection_path, config):
    """Skip the plugin's own source files; they aren't test modules.

    pytest 8+ with ``--import-mode=importlib`` walks up from each test
    file looking for the closest package; without this hook it tries to
    import ``__init__.py`` directly and fails on the relative ``from .``
    imports.
    """
    # Anything at the root level of the plugin is plugin source, not
    # tests. ``tests/`` lives one level down and is the canonical test
    # directory.
    plugin_root = _PLUGIN_DIR
    if collection_path.parent == plugin_root:
        return collection_path.suffix == ".py"
    return False


# Pre-load the plugin package so anything that asks for
# ``browser_policy_router`` in a test gets the importlib-loaded instance.
_PLUGIN_DIR = Path(__file__).resolve().parent
if "browser_policy_router" not in sys.modules:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "browser_policy_router",
        _PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(_PLUGIN_DIR)],
    )
    if spec is not None and spec.loader is not None:
        mod = importlib.util.module_from_spec(spec)
        sys.modules["browser_policy_router"] = mod
        spec.loader.exec_module(mod)
