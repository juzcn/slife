"""credstore — standalone cross-platform credential storage.

Re-exports from slife.credstore so ``import credstore`` works
independently without importing the rest of slife.
"""

from slife.credstore import *  # noqa: F401 F403
from slife.credstore import __all__, __version__  # noqa: F401
