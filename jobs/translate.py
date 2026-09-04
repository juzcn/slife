"""Translate sample job — the reference example for the job-coding plugin.

A job is a plain function; its name becomes the tool name, its docstring
the description, its typed signature the parameters schema.  Jobs that call
the LLM are ``async def`` and ``await llm.chat(...)`` — one narrow one-shot
chat built solely from the declared arguments, with no conversation history
or system prompt from the host.
"""

# ``llm`` binds to the currently-executing job's model.
from slife.plugins.job_coding import llm


async def translate(text: str, lang: str = "zh", model: str = "") -> str:
    """Translate text into a target language.

    Args:
        text: Source text to translate.
        lang: Target language code (defaults to zh).
        model: Optional model ref (provider/model) to run on, e.g.
            "deepseek/deepseek-v4-flash". Empty uses job_coding_model.
    """
    return await llm.chat(
        system=(
            "You are a professional translator. Output only the "
            "translation, with no explanations or notes."
        ),
        user=f"Translate the following into {lang}:\n{text}",
        model=model or None,
    )