from __future__ import annotations

import re


def maybe_answer_atm_telegram_request(user_goal: str) -> str | None:
    text = user_goal or ""
    low = text.lower()
    route = re.search(r"\batm\s*:\s*([^-–—>]+)\s*[-–—>]\s*(.+)$", text, flags=re.I)
    if route:
        try:
            from openshell_backend.atm_telegram import build_named_plan, render_reply

            return render_reply(build_named_plan(route.group(1).strip(), route.group(2).strip()))
        except Exception as exc:
            return f"ATM: non riesco a calcolare il percorso ({exc}). Controlla che origine e destinazione siano salvate nella GUI."

    trigger_patterns = [
        r"\\batm\\b",
        r"\\bautobus\\b",
        r"\\bbus\\b",
        r"\\btram\\b",
        r"\\bmetro\\b",
        r"\\btelegram\\b",
        r"\\bposizione\\b",
        r"\\bgps\\b",
        r"\\barci\\s+bellezza\\b",
        r"\\bpiscina\\s+suzzani\\b",
        r"\\bgiromilano\\b",
    ]
    if not any(re.search(pattern, low, flags=re.I) for pattern in trigger_patterns):
        return None
    return (
        "Skill ATM Telegram pronta.\n"
        "GUI: http://127.0.0.1:19090/atm-telegram\n"
        "Uso Telegram: invia location allegata + testo destinazione, es. 'arci bellezza' o 'piscina suzzani'.\n"
        "API: POST /atm-telegram/telegram-webhook con message.location.latitude/longitude e text/caption destinazione.\n"
        "Output: fermata vicina, distanza, linee OSM filtrate, attese live se GiroMilano via browser risponde, alternative, link ATM.\n"
        "Nota: 'casa' va salvata dalla GUI prima dell'uso."
    )
