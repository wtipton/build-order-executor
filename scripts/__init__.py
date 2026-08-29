"""One-off tools that need a live game: the data dumps behind reference/*.md.

Not part of the bot. They live in a package (rather than as loose files) so they can
import the bot's modules while being run from the repo root:

    python -m scripts.economy_measure --target linux --slice 0:12

INVARIANT: nothing outside this package may import it. Each tool launches its own game
(via run.py) rather than being hooked into the bot, so scripts/ can be dropped from the
Docker image without touching the bot -- the tools are run against a bind-mounted repo
(`docker run -v "$PWD":/app ...`) when they are needed. tests/test_layout.py enforces it.
"""
