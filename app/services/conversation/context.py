"""References come only from scoped tool results; model metadata cannot grant access."""

from datetime import UTC, datetime, timedelta

TOPICS = {
    "finance_query": "finance",
    "record_transaction": "finance",
    "day_plan": "planning",
    "save_planning": "planning",
    "save_work_schedule": "planning",
    "purchase_history": "shopping",
    "calendar_change": "calendar",
}


def update_context(current, evidence, *, answer=None, history_days=7, clear_question=False):
    now = datetime.now(UTC)
    cutoff = (now - timedelta(days=history_days)).isoformat()
    state = {
        k: v
        for k, v in (current or {}).items()
        if isinstance(v, dict) and v.get("at", "") >= cutoff
    }
    for item in evidence:
        result, tool = item["result"], item["tool"]
        if result.get("error"):
            continue
        records = result.get("records", []) or ([result["record"]] if result.get("record") else [])
        topics = {TOPICS[tool]: records} if tool in TOPICS else {}
        for kind, topic in (("shopping", "shopping"), ("task", "tasks")):
            matching = [record for record in records if record.get("kind") == kind]
            if matching or result.get("kind") == kind:
                topics[topic] = matching
        for topic, topic_records in topics.items():
            value = {"at": now.isoformat(), "last_tool": tool}
            if topic_records or "records" in result:
                value["references"] = [
                    {k: r[k] for k in ("id", "title", "kind") if k in r} for r in topic_records[:12]
                ]
            for key in ("start_date", "end_date", "source", "day"):
                if key in result:
                    value[key] = result[key]
            state[topic] = {**state.get(topic, {}), **value}
            if clear_question:
                state["dialogue"] = {"at": now.isoformat(), "topic": topic, "question": None}
    if answer is not None:
        state["dialogue"] = {
            "at": now.isoformat(),
            "topic": answer.topic,
            "question": answer.reply if answer.needs_clarification else None,
        }
    return state
