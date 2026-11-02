"""Deprecated import alias for Hydraweave."""
import importlib
import importlib.abc
import importlib.util
import sys
import warnings

import hydraweave as _implementation
from hydraweave import *  # noqa: F401,F403
from hydraweave import __version__

warnings.warn("hyperloom is deprecated; import hydraweave instead", DeprecationWarning, stacklevel=2)


def __getattr__(name):
    if name.startswith("__"):
        raise AttributeError(name)
    try:
        return getattr(_implementation, name)
    except AttributeError:
        return importlib.import_module("hydraweave." + name)


class _AliasLoader(importlib.abc.Loader):
    def __init__(self, target):
        self.target = target

    def create_module(self, spec):
        return importlib.import_module(self.target)

    def exec_module(self, module):
        pass


class _AliasFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith("hyperloom.") or fullname == "hyperloom.__main__":
            return None
        renamed = "hydraweave" + fullname[len("hyperloom"):]
        spec = importlib.util.find_spec(renamed)
        if spec is None:
            return None
        return importlib.util.spec_from_loader(fullname, _AliasLoader(renamed),
                                              is_package=spec.submodule_search_locations is not None)


sys.meta_path.insert(0, _AliasFinder())
