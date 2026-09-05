"""Search the web for news using the Baidu AI Search skill.

Reads the baidu-search skill's search.py via subprocess and returns a
curated news list (title, source, date, url, snippet).
"""

import json
import os
import subprocess
import sys


def baidu_news(query: str = "", count: int = 5, freshness: str = "") -> str:
    """Search the web with the Baidu AI Search skill and return news results.

    Args:
        query: Search query text. Empty defaults to finding the requested news topic.
        count: Number of results to return (1-50).
        freshness: Time filter: '' (any), 'pd'/'pw'/'pm'/'py' (day/week/month/year),
            or an explicit range like '2026-09-01to2026-09-05'.
    """
    q = query.strip() or "首都经济贸易大学 新闻"
    request: dict = {"query": q, "count": max(1, min(int(count), 50))}
    if freshness:
        request["freshness"] = freshness

    script = _find_search_script()
    if script is None:
        return "Error: baidu-search skill script not found (skills/baidu-search/scripts/search.py)"

    try:
        proc = subprocess.run(
            [sys.executable, script, json.dumps(request, ensure_ascii=False)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            env={**os.environ},
        )
    except FileNotFoundError as e:
        return f"Error: python not found: {e}"
    except subprocess.TimeoutExpired:
        return "Error: baidu search timed out after 60s"

    if proc.returncode != 0:
        return f"Error: search script failed ({proc.returncode}): {proc.stderr.strip()[:300] or proc.stdout.strip()[:300]}"

    out = proc.stdout.strip()
    # The script echoes "success parse request body: {...}" then a JSON array.
    brace = out.find("[")
    if brace < 0:
        return f"Error: unexpected output: {out[:300]}"
    try:
        items = json.loads(out[brace:])
    except json.JSONDecodeError as e:
        return f"Error: unparsable search output: {e}"

    if not items:
        return f"没有找到与「{q}」相关的新闻结果。"

    lines = [f"百度新闻搜索: 「{q}」 (共{len(items)}条)", ""]
    for i, item in enumerate(items, 1):
        title = (item.get("title") or "").strip()
        website = (item.get("website") or "").strip()
        date = (item.get("date") or "").strip()
        url = (item.get("url") or "").strip()
        content = (item.get("content") or "").strip().replace("\n", " ")
        snippet = content[:120] + ("…" if len(content) > 120 else "")
        lines.append(f"{i}. {title}")
        tags = " | ".join(x for x in (website, date) if x)
        if tags:
            lines.append(f"   {tags}")
        if url:
            lines.append(f"   {url}")
        if snippet:
            lines.append(f"   {snippet}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _find_search_script() -> str | None:
    """Locate skills/baidu-search/scripts/search.py under the data/workspace dirs."""
    candidates = [
        os.path.join(os.getcwd(), "skills", "baidu-search", "scripts", "search.py"),
        os.path.join(os.path.dirname(os.getcwd()), "skills", "baidu-search", "scripts", "search.py"),
        os.path.expanduser("~/.slife/skills/baidu-search/scripts/search.py"),
    ]
    for cand in candidates:
        if os.path.exists(cand):
            return cand
    return None
