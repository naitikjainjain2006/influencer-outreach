"""Niche classification via a configurable keyword lexicon (deterministic, explainable)."""
import json
import re
from collections import Counter
from functools import lru_cache


@lru_cache(maxsize=512)
def _rx(words: tuple) -> re.Pattern:
    return re.compile(r"\b(?:%s)\b" % "|".join(re.escape(w) for w in words), re.I)


def classify(rec: dict, lexicon: dict) -> dict:
    """Returns {niche, relevance_score, themes}.

    relevance = 0.7 * (share of recent videos whose title matches any niche keyword)
              + 0.3 * (1 if channel description/keywords match).
    With no video titles (e.g. CSV imports) relevance is 0.5 if the bio matches, else 0.
    """
    titles = json.loads(rec.get("recent_titles") or "[]")
    channel_text = f"{rec.get('description') or ''} {rec.get('keywords') or ''}"
    theme_counts: Counter = Counter()
    group_title_hits = {g: 0 for g in lexicon}
    group_text_hits = {g: 0 for g in lexicon}
    any_title_hits = 0

    for t in titles:
        matched_any = False
        for g, themes in lexicon.items():
            hit = False
            for theme, words in themes.items():
                if _rx(tuple(words)).search(t):
                    theme_counts[theme] += 1
                    hit = True
            if hit:
                group_title_hits[g] += 1
                matched_any = True
        any_title_hits += matched_any

    text_match = False
    for g, themes in lexicon.items():
        for theme, words in themes.items():
            if _rx(tuple(words)).search(channel_text):
                theme_counts[theme] += 1
                group_text_hits[g] += 1
                text_match = True

    n = len(titles)
    if n:
        relevance = 0.7 * (any_title_hits / n) + 0.3 * (1 if text_match else 0)
    else:
        relevance = 0.5 if text_match else 0.0

    ratios = {g: (group_title_hits[g] / n if n else 0.0) for g in lexicon}
    strong = [g for g, r in ratios.items() if r >= 0.2]
    if len(strong) >= 2 and set(strong) == set(lexicon):
        niche = " & ".join(sorted(lexicon, key=lambda g: g != "Fashion"))  # "Fashion & Beauty"
    elif strong:
        niche = max(strong, key=lambda g: ratios[g])
    elif text_match:
        niche = max(group_text_hits, key=group_text_hits.get)
    else:
        niche = "Unclassified"

    return {
        "niche": niche,
        "relevance_score": round(relevance, 2),
        "themes": json.dumps([t for t, _ in theme_counts.most_common(4)]),
    }
