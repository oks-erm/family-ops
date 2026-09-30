"""Only complete commands bypass language understanding."""

import re
from datetime import date

from app.schemas.conversation import CreateRecords, DayPlan, ListRecords


def deterministic_request(text: str, today: date):
    normalized = text.strip().casefold()
    if normalized in {"shopping list", "show shopping list"}:
        return "list_records", ListRecords(kind="shopping").model_dump(mode="json")
    if normalized in {"show my tasks", "list tasks"}:
        return "list_records", ListRecords(kind="tasks").model_dump(mode="json")
    if normalized in {"today's plan", "show today's plan"}:
        return "day_plan", DayPlan(day=today).model_dump(mode="json")
    # Explicit command grammar, not broad keywords such as 'need' or 'buy'.
    match = re.fullmatch(r"(shopping|task):\s*([^\n;?!]{1,255})", text.strip(), re.I)
    if match:
        title = match[2].strip()
        if not title or re.search(r"\b(and|then|tomorrow|today|at|from)\b", title, re.I):
            return None
        args = CreateRecords(
            items=[
                {"kind": "shopping" if match[1].lower() == "shopping" else "task", "title": title}
            ]
        )
        return "create_records", args.model_dump(mode="json")
    return None


def render_result(result):
    if result.get("message") or result.get("error"):
        return str(result.get("message") or result["error"])
    if "records" in result:
        lines = [f"{result['total']} pending item(s):"]
        lines += [
            f"• {r['title']}"
            + (f" — {r['store']}" if r.get("store") else "")
            + (f" — {r['due_date']}" if r.get("due_date") else "")
            for r in result["records"]
        ]
        if result.get("next_offset") is not None:
            lines.append("Ask for the next page to see more.")
        return "\n".join(lines)
    if "totals" in result:
        lines = [f"{result['source']}: {result['start_date']} to {result['end_date']}"]
        lines += [f"{r['amount']} {r['currency']} ({r['count']} records)" for r in result["totals"]]
        if not result["totals"]:
            lines.append("No matching records.")
        if not result["complete"]:
            lines.append(f"Incomplete: {result['invalid_amounts']} invalid amount(s) excluded.")
        return "\n".join(lines)
    if "day" in result:
        lines = [
            f"Plan for {result['day']} ({result['timezone']})",
            "Calendar (last synchronized data):",
        ]
        lines += [f"• {e['title']} — {e['start']} to {e['end']}" for e in result["events"]]
        lines += ["Tasks:"] + [f"• {t['title']}" for t in result["tasks"]]
        if result.get("truncated"):
            lines.append("This view is truncated; narrow the request.")
        return "\n".join(lines)
    return "The request completed."
