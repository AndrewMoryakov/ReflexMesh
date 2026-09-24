"""V0.3 rules baseline: fixed RU/EN keyword stems, frozen with the protocol.

Score each allowed route by the number of distinct stems found in the lowercased goal.
The unique highest positive score wins; no match or a tie abstains.
"""

KEYWORDS = {
    "CUA": [
        "открой", "перейди", "нажми", "кликни", "заполни", "введи", "вставь", "отметь",
        "прокрути", "перетащи", "загрузи", "закрой", "выбери", "отправь",
        "open", "go to", "navigate", "click", "type ", "fill", "enter ", "select", "switch",
        "scroll", "drag", "upload", "close", "submit",
    ],
    "PERCEPTION": [
        "скриншот", "снимк", "снимок", "фото", "изображени", "на экране", "прочитай",
        "перепиши", "распознай", "извлеки",
        "screenshot", "screen capture", "image", "photo", "read ", "transcribe", "extract",
        "visible", "shown",
    ],
    "LLM": [
        "напиши", "составь", "придумай", "объясни", "переведи", "резюме", "сравни",
        "сгенерируй", "план", "функци",
        "write", "draft", "explain", "summarize", "translate", "generate", "compare",
        "recommend", "plan", "code", "function",
    ],
}


def route(goal: str, allowed: list[str]) -> str | None:
    text = goal.lower()
    scores = {r: sum(1 for stem in KEYWORDS[r] if stem in text) for r in allowed if r in KEYWORDS}
    if not scores:
        return None
    best = max(scores.values())
    winners = [r for r, s in scores.items() if s == best]
    return winners[0] if best > 0 and len(winners) == 1 else None
