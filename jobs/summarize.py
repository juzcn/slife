"""Summarize sample job — concise summary of a long text.

Same conventions as translate.py: one public function = one job tool; the
LLM call is a single, explicit one-shot chat with no host context.
"""

from slife.plugins.job_coding import llm


async def summarize(text: str, max_words: int = 100) -> str:
    """Summarize a text into a concise summary.

    Args:
        text: The text to summarize.
        max_words: Approximate maximum length of the summary.
    """
    return await llm.chat(
        system=(
            "You are an expert summarizer. Output only the summary, "
            "no preamble or commentary."
        ),
        user=f"Summarize the following in at most {max_words} words:\n{text}",
    )