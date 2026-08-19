"""Safe Content builders shared across the TUI.

Two rendering paths exist for ``textual.content.Content``:

* ``mc`` — builds from a **controlled** markup string.  Only use for
  strings we construct ourselves (labels, section headers).  Never pass
  user data or tool output through this function.
* ``lit`` — builds from arbitrary text, NEVER parsed as markup.  This is
  the safe path for all user data: command output, search results, file
  contents, tool names, arg values.  Characters like ``&``, ``[``, ``]``
  are rendered literally.

Using the wrong path on user data raises ``MarkupError`` (e.g. search
results containing URLs or JSON), so both helpers stay in one place.
"""

from textual.content import Content


def mc(text: str) -> Content:
    """Build Content from a **controlled** markup string.

    Only use for strings we construct ourselves (labels, section headers).
    Never pass user data or tool output through this function.
    """
    return Content.from_markup(text)


def lit(text: str, style: str = "") -> Content:
    """Build Content from arbitrary text — NEVER parsed as markup.

    This is the safe path for all user data: command output, search results,
    file contents, tool names, arg values.  Characters like &, [, ] are
    rendered literally.
    """
    c = Content.from_text(text, markup=False)
    if style:
        c = c.stylize(style)
    return c
