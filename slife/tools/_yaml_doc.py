"""Comment-preserving YAML writes — edit the document, don't re-serialize it.

``write_config`` used to parse a config into a dict and dump the dict back,
which silently deleted every comment in the file.  For slife's configs that is
most of their value: ``sharefile.yaml`` is 77% comments, ``local_embed.yaml``
65% — they document ``enabled`` / ``autoload`` and each section's meaning for a
human hand-editing them.

So a write now edits the **document**: the file's current text is loaded as a
round-trip document, the difference from the incoming dict is applied to that
document, and the document is dumped.  Non-data elements — comments,
indentation, blank lines, key order, quote style — survive because they were
never round-tripped through a dict.  This is the same shape ``tomlkit`` gives
Poetry's ``pyproject.toml``: *modify the document, never ``unwrap()`` it*.

ruamel.yaml's round-trip mode preserves all of that natively, so this module is
a thin policy layer over it — the four settings in :func:`new_yaml` are the
whole trick, plus the delta walk in :func:`_apply`.  Its predecessor
(``_json5_doc.py``) hand-rolled a whitespace model instead, because json-five's
model API is unstable by its own author's warning and it ships no validation.
"""

from __future__ import annotations

import io

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap


def new_yaml() -> YAML:
    """A round-trip ``YAML`` with slife's house style applied.

    Each setting undoes a ruamel default that is wrong for these files:

    - **preserve_quotes** — the file's own quote style survives a rewrite.
    - **allow_unicode** — without it non-ASCII is dumped as ``\\uXXXX``
      escapes, so every Chinese description in the configs would change
      character between writes.
    - **width** — ruamel wraps at 80 columns and will break a long quoted
      scalar inside the string.  The value survives, but the file churns on
      every write.
    - **indent** — the sequence style the configs already use (``- key:
      value`` indented under its parent key, 2-space maps).
    """
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.allow_unicode = True
    yaml.width = 4096
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def render(value) -> str:
    """Render *value* as config text from scratch — no document to preserve.

    The path for a file being created, and the fallback when the existing text
    cannot be edited in place.
    """
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
            # would move a comment onto the wrong element.  A scalar keeps the
            # comment beside it — assignment replaces the value, not the node.
            doc[key] = value


def render_document(current: str, new: dict) -> str:
    """Render *new* as config text, preserving *current*'s non-data elements.

    *current* is the file's existing text (``""`` for a file being created).
    Returns the text to write.  Falls back to a plain :func:`render` when the
    current text cannot be loaded as a document or when the edited document
    does not read back as *new* — losing comments is bad, writing a config that
    says something else is worse.
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
                # coerces — an unquoted timestamp in a hand-edited file loads
                # as a datetime — compares equal instead of forcing a
                # comment-losing fallback.
                if yaml.load(edited) == yaml.load(render(new)):
                    return edited
        except Exception:
            pass
    return render(new)
