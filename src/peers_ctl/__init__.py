"""peers-ctl: multi-project controller for peers loops.

Sits one level above `peers`: registers target projects, starts/stops
detached `peers run` processes against them, surfaces status across
all of them.
"""

# Same package as `peers` (see pyproject.toml [project.name]), so we
# pull the version from the same metadata entry — single source of
# truth, in sync on bump.
from peers import __version__

__all__ = ["__version__"]
