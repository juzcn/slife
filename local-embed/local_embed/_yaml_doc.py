"""Comment-preserving YAML writes — edit the document, don't re-serialize it.

A deliberate copy of ``slife/tools/_yaml_doc.py``.  local-embed is a standalone
distribution — slife depends on *it*, never the reverse — so the two packages
cannot share the module, and a few dozen lines of policy is a cheaper price
than a dependency edge pointing the wrong way.

``write_config`` used to serialise the whole dict, and ``local_embed.yaml`` is
65% comments: the header prose describing the config path, the per-model
``autoload`` note, the ``~``-expansion note on ``gguf_path``.  Every
``local-embed set`` dropped all of them.  A write now edits the **document**
instead: the file's current text is loaded, the difference from the incoming
dict is applied to it, and the document is dumped — so comments, indentation,
key order and quote style survive because they were never round-tripped through
a dict.  This is the same shape ``tomlkit`` gives Poetry's ``pyproject.toml``.
"""

from __future__ import annotations

import io

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap


def new_yaml() -> YAML:
    """A round-trip ``YAML`` with the house style applied.

    Each setting undoes a ruamel default that is wrong for this file:

    - **preserve_quotes** — the file's own quote style survives a rewrite.
    - **allow_unicode** — without it non-ASCII is dumped as ``\\uXXXX``
      escapes, so the em dashes in the header would change character.
    - **width** — ruamel wraps at 80 columns and will break a long quoted
      scalar inside the string; the value survives but the file churns.
    - **indent** — 2-space maps with ``- key: value`` sequences indented under
      their parent key.
    """
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.allow_unicode = True
    yaml.width = 4096
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def render(value) -> str:
    """Render *value* as config text from scratch — no document to preserve."""
    yaml = new_yaml()
    buf = io.StringIO()
    yaml.dump(value, buf)
    return buf.getvalue()


def _apply(doc, old: dict, new: dict) -> None:
    """Make *doc* render *new*, touching only what differs from *old*.

    Assigning into the document rather than rebuilding it is what keeps the
    comments: ruamel attaches each comment to the node it surrounds, so a node
    this walk never touches keeps its own.

    *old* is the live document in the recursion and the caller's loaded copy at
    the top — comparing against it is safe because every mutation happens on a
    key whose disposition has already been decided.
    """
    for key in [k for k in old if k not in new]:
        del doc[key]

    for key, value in new.items():
        if key not in old:
            doc[key] = value
            continue
        if old[key] == value:
            continue
        current = doc[key]
        if (isinstance(value, dict) and isinstance(old[key], dict)
                and isinstance(current, CommentedMap)):
            _apply(current, old[key], value)
        else:
            # A list is replaced wholesale: element-wise identity is not
            # reliable (reordered / re-edited entries) and a wrong guess here
            # would move a comment onto the wrong element.
            doc[key] = value


def render_document(current: str, new: dict) -> str:
    """Render *new* as config text, preserving *current*'s non-data elements.

    *current* is the file's existing text (``""`` for a file being created).
    Falls back to a plain :func:`render` when the current text cannot be loaded
    as a document or when the edited document does not read back as *new* —
    losing comments is bad, writing a config that says something else is worse.
    """
    if current.strip() and isinstance(new, dict):
        try:
            yaml = new_yaml()
            doc = yaml.load(current)
            if isinstance(doc, CommentedMap):
                _apply(doc, doc, new)
                buf = io.StringIO()
                yaml.dump(doc, buf)
                edited = buf.getvalue()
                # Both sides go through the same loader, so a value the loader
                # coerces compares equal instead of forcing a comment-losing
                # fallback.
                if yaml.load(edited) == yaml.load(render(new)):
                    return edited
        except Exception:
            pass
    return render(new)
