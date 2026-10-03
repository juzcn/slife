"""Allow running as: ``python -m slife [--agent <id>] [--lang en|zh]``.

A thin door onto the console script's entry point (``slife:main``), so
``python -m slife`` and ``slife`` agree on every flag.
"""

from slife import main

if __name__ == "__main__":
    main()
