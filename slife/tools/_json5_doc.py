"""Comment-preserving JSON5 writes — edit the document, don't re-serialize it.

``write_config`` used to parse a config into a dict and dump the dict back,
which silently deleted every ``//`` comment in the file.  For slife's configs
that is most of their value: ``tools.json5`` is 19% comments, ``local_embed
.json5`` 64%, ``sharefile.json5`` 76% — they document ``enabled`` / ``autoload``
and each section's meaning for a human hand-editing them.

So a write now edits the **document** instead: the file's current text is
loaded as a json-five model, the difference from the incoming dict is applied
to that model, and the model is dumped.  Non-data elements — comments,
indentation, blank lines, key order, quote style — survive because they were
never round-tripped through a dict.  This is the same shape ``tomlkit`` gives
Poetry's ``pyproject.toml``: *modify the document, never ``unwrap()`` it*.

Three things the library does NOT do, which this module does:

- **json-five's model API is explicitly unstable** ("breaking changes, even in
  minor releases"), so the model manipulation is confined to this file.
- **It performs no validation** — "no validation to ensure your model edits
  won't result in invalid JSON5 when dumped" (its own README).  Every render
  here is re-parsed before it is returned, and the caller falls back to a
  plain render if it doesn't survive.
- **Its dumper escapes non-ASCII and quotes every key**, which is wrong for
  these files twice over: the configs carry Chinese text that
  ``json5.dumps(..., ensure_ascii=False)`` used to keep readable, and their
  house style is bare keys.  So new text is rendered by :func:`render` here,
  not by the library, and only *parsed* by it — a rendered fragment is fed
  through ``ModelLoader`` so it arrives carrying real whitespace, the trick
  that lets an added entry be emitted formatted instead of on one bare line.
"""

from __future__ import annotations

import json
import re
import textwrap

try:
    import json5
    import json5.model as M
    from json5.dumper import ModelDumper
    from json5.loader import ModelLoader
except ImportError as exc:  # pragma: no cover — a broken install, not a branch
    # `json5` and `json-five` both ship a top-level `json5/` package, so
    # whichever installs last overwrites the other's `__init__.py` and
    # `parser.py` — and only json-five's parser has `parse_source`, which its
    # own loader imports.  The raw failure names neither distribution.
    raise ImportError(
        "slife's config writer needs json-five, but the json5 distribution "
        "(via sys-lang -> is-unicode-supported -> is-legacy-terminal) claims "
        "the same import name and overwrote part of it.  Uninstall json5; "
        "pyproject.toml's [tool.uv] override-dependencies drops it from this "
        "project's lock."
    ) from exc

#: An ECMAScript identifier — the keys a JSON5 file may leave unquoted.
#: Anything else (``rest-api``, ``tavily-mcp``) must be quoted or the dump is
#: not valid JSON5; json-five does not check this for you.
_BARE_KEY = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")

#: One indent step, matching the 2 spaces every slife config uses (and the
#: ``indent=2`` the old dict-dump wrote).
_INDENT = "  "


# ═══════════════════════════════════════════════════════════════════════
# Rendering — the one dict → JSON5-text path (replaces json5.dumps(dict))
# ═══════════════════════════════════════════════════════════════════════


def _key(name: str) -> str:
    return name if _BARE_KEY.match(name) else json.dumps(name, ensure_ascii=False)


def _scalar(value) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    raise TypeError(f"cannot render {type(value).__name__} as JSON5")


def render(value, level: int = 0) -> str:
    """Render *value* as JSON5 text — bare keys, non-ASCII kept literal.

    Deliberately not ``json5.dumps``: json-five's dumper escapes non-ASCII
    (``\\u4e2d\\u6587``) and quotes every key, so a config written through it
    would change character between writes.  This keeps the house style the
    files already have and the old ``ensure_ascii=False`` guaranteed.
    """
    pad, inner = _INDENT * level, _INDENT * (level + 1)
    if isinstance(value, dict):
        if not value:
            return "{}"
        body = ",\n".join(
            f"{inner}{_key(k)}: {render(v, level + 1)}" for k, v in value.items()
        )
        return "{\n" + body + f"\n{pad}}}"
    if isinstance(value, (list, tuple)):
        if not value:
            return "[]"
        body = ",\n".join(f"{inner}{render(v, level + 1)}" for v in value)
        return "[\n" + body + f"\n{pad}]"
    return _scalar(value)


def _fragment(value, indent: str):
    """A model node for *value*, formatted as if it sat at *indent*.

    The round-trip through text is the point: a node built by hand carries no
    whitespace, so the dumper emits it bare (``{command:"echo"}`` on one
    line).  Text that was parsed has real ``wsc_before``/``wsc_after``.
    """
    if not isinstance(value, (dict, list)) or not value:
        return _leaf(value)
    text = textwrap.indent(render(value), indent).lstrip()
    return json5.loads(text, loader=ModelLoader()).value


def _leaf(value):
    """A node for a scalar, or an empty container."""
    if value is None:
        return M.NullLiteral()
    if value is True or value is False:
        return M.BooleanLiteral(value=value)
    if isinstance(value, int):
        return M.Integer(raw_value=str(value))
    if isinstance(value, float):
        return M.Float(raw_value=repr(value))
    if isinstance(value, str):
        return M.DoubleQuotedString(
            characters=value, raw_value=json.dumps(value, ensure_ascii=False),
        )
    if isinstance(value, list):
        return M.JSONArray()
    if isinstance(value, dict):
        return M.JSONObject()
    raise TypeError(f"cannot build a node for {type(value).__name__}")


def _key_node(name: str):
    if _BARE_KEY.match(name):
        return M.Identifier(name=name, raw_value=name)
    return M.DoubleQuotedString(
        characters=name, raw_value=json.dumps(name, ensure_ascii=False),
    )


# ═══════════════════════════════════════════════════════════════════════
# Applying a dict delta onto the document model
# ═══════════════════════════════════════════════════════════════════════


def _inherit(old_node, new_node):
    """Carry the replaced node's surrounding whitespace/comments across."""
    new_node.wsc_before = list(old_node.wsc_before)
    new_node.wsc_after = list(old_node.wsc_after)
    return new_node


def _tail_ws(obj) -> list:
    """Whitespace and comments between the last value and the closing brace.

    Held by the last VALUE's ``wsc_after`` — moving it is what keeps a comment
    that documented the end of an object from ending up mid-object when a
    member is appended.
    """
    return list(obj.values[-1].wsc_after) if obj.values else []


def _member_indent(obj, fallback: str = _INDENT) -> str:
    """The indent new members of *obj* should use, read off an existing one."""
    if len(obj.keys) > 1:
        ws = obj.keys[-1].wsc_before
        if ws and isinstance(ws[0], str) and "\n" in ws[0]:
            return ws[0].rsplit("\n", 1)[1]
    tail = _tail_ws(obj)
    if tail and isinstance(tail[0], str) and "\n" in tail[0]:
        return tail[0].rsplit("\n", 1)[1]
    leading = obj.leading_wsc
    if leading and isinstance(leading[0], str) and "\n" in leading[0]:
        return leading[0].rsplit("\n", 1)[1] + _INDENT
    return fallback


def _set_leaf(node, value):
    """Set a scalar in place when the node type allows — nothing else moves."""
    if isinstance(node, M.BooleanLiteral) and isinstance(value, bool):
        node.value = value
        return node
    if isinstance(node, M.String) and isinstance(value, str):
        node.characters = value
        node.raw_value = json.dumps(value, ensure_ascii=False)
        return node
    if isinstance(node, M.Integer) and isinstance(value, int) and not isinstance(value, bool):
        node.raw_value = str(value)
        return node
    if isinstance(node, M.Float) and isinstance(value, float):
        node.raw_value = repr(value)
        return node
    if isinstance(node, M.NullLiteral) and value is None:
        return node
    return _inherit(node, _leaf(value))


def _apply_object(obj, old: dict, new: dict) -> None:
    """Make *obj* render *new*, touching only what differs from *old*."""
    for key in [k for k in old if k not in new]:
        i = obj.keys.index(key)
        gone, was_last = obj.values[i], i == len(obj.values) - 1
        del obj.keys[i]
        del obj.values[i]
        # The removed member held the whitespace before the brace; the new
        # last member inherits it, or the object would close on the same line.
        if was_last and obj.values:
            obj.values[-1].wsc_after = list(gone.wsc_after)

    for key, value in new.items():
        if key in old:
            continue
        tail = _tail_ws(obj)
        if obj.values:
            obj.values[-1].wsc_after = []
        indent = _member_indent(obj)
        kn = _key_node(key)
        kn.wsc_before = [] if not obj.keys else ["\n" + indent]
        vn = _fragment(value, indent)
        vn.wsc_before = [" "]
        vn.wsc_after = tail or ["\n" + indent[: -len(_INDENT)]]
        obj.keys.append(kn)
        obj.values.append(vn)

    for key, value in new.items():
        if key not in old or old[key] == value:
            continue
        i = obj.keys.index(key)
        current = obj.values[i]
        if (isinstance(value, dict) and isinstance(old[key], dict)
                and isinstance(current, M.JSONObject)):
            _apply_object(current, old[key], value)
        elif (isinstance(value, list) and isinstance(old[key], list)
                and isinstance(current, M.JSONArray)):
            # A list is replaced wholesale: element-wise identity is not
            # reliable (reordered / re-edited entries) and a wrong guess here
            # would move a comment onto the wrong element.
            obj.values[i] = _inherit(current, _fragment(value, _member_indent(obj)))
        else:
            obj.values[i] = _set_leaf(current, value)


# ═══════════════════════════════════════════════════════════════════════
# Public entry point
# ═══════════════════════════════════════════════════════════════════════


def render_document(current: str, new: dict) -> str:
    """Render *new* as JSON5 text, preserving *current*'s non-data elements.

    *current* is the file's existing text (``""`` for a file being created).
    Returns the text to write.  Falls back to a plain :func:`render` when the
    current text cannot be loaded as a document or when the edited document
    does not read back as *new* — losing comments is bad, writing a config
    that says something else is worse.
    """
    if current.strip():
        try:
            model = json5.loads(current, loader=ModelLoader())
            old = json5.loads(current)
            if isinstance(old, dict) and isinstance(new, dict):
                _apply_object(model.value, old, new)
                # Driven directly rather than through ``json5.dumps``: that
                # signature is typed ``BaseDumper``, and ModelDumper is
                # standalone (it creates its own Environment), so passing it
                # is a type error the runtime doesn't share.  These three
                # lines are exactly what ``dumps`` does with it.
                dumper = ModelDumper()
                dumper.dump(model)
                dumper.env.outfile.seek(0)
                edited = dumper.env.outfile.read()
                # json-five validates nothing; this is the guard it warns you
                # to write yourself.
                if json5.loads(edited) == new:
                    return edited
        except Exception:
            pass
    return render(new)
