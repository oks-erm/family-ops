from dataclasses import dataclass
from datetime import date, datetime, time
from uuid import UUID


@dataclass(frozen=True)
class CalendarEventInput:
    title: str
    starts_at: datetime
    ends_at: datetime
    location: str | None = None


@dataclass(frozen=True)
class PlanningInput:
    user_id: UUID
    plan_date: date
    work_start: time | None
    work_end: time | None
    unusual_notes: str | None
    tasks: list[str]
    shopping_items: list[str]
    calendar_events: list[CalendarEventInput]


class PlanningService:
    def build_daily_plan(self, planning_input: PlanningInput) -> dict[str, object]:
        fixed_events = self._fixed_events(planning_input)
        free_windows = self._free_windows(planning_input, fixed_events)
        return {
            "date": planning_input.plan_date.isoformat(),
            "work_block": {
                "start": planning_input.work_start.strftime("%H:%M") if planning_input.work_start else None,
                "end": planning_input.work_end.strftime("%H:%M") if planning_input.work_end else None,
            },
            "notes": planning_input.unusual_notes,
            "fixed_events": fixed_events,
            "free_windows": free_windows,
            "suggested_tasks": self._suggest_tasks(planning_input.tasks, free_windows),
            "shopping": planning_input.shopping_items,
        }

    def render_plan_message(self, plan: dict[str, object]) -> str:
        lines = [f"Plan for {plan['date']}"]
        work_block = plan.get("work_block") or {}
        if isinstance(work_block, dict) and (work_block.get("start") or work_block.get("end")):
            lines.append(f"Work: {work_block.get('start') or '?'}-{work_block.get('end') or '?'}")
        if plan.get("notes"):
            lines.append(f"Notes: {plan['notes']}")

        fixed_events = plan.get("fixed_events") or []
        if fixed_events:
            lines.append("")
            lines.append("Fixed events")
            for event in fixed_events:
                lines.append(f"- {event['start']}-{event['end']}: {event['title']}")

        free_windows = plan.get("free_windows") or []
        if free_windows:
            lines.append("")
            lines.append("Free windows")
            for window in free_windows:
                lines.append(f"- {window['start']}-{window['end']}")

        tasks = plan.get("suggested_tasks") or []
        if tasks:
            lines.append("")
            lines.append("Suggested tasks")
            for task in tasks:
                title = task["title"] if isinstance(task, dict) else str(task)
                lines.append(f"- {title}")

        shopping = plan.get("shopping") or []
        if shopping:
            lines.append("")
            lines.append("Shopping")
            for item in shopping:
                lines.append(f"- {item}")
        return "\n".join(lines)

    def _fixed_events(self, planning_input: PlanningInput) -> list[dict[str, str]]:
        events = []
        if planning_input.work_start and planning_input.work_end:
            events.append(
                {
                    "title": "Work",
                    "start": planning_input.work_start.strftime("%H:%M"),
                    "end": planning_input.work_end.strftime("%H:%M"),
                    "source": "work",
                }
            )
        for event in planning_input.calendar_events:
            events.append(
                {
                    "title": event.title,
                    "start": event.starts_at.strftime("%H:%M"),
                    "end": event.ends_at.strftime("%H:%M"),
                    "source": "calendar",
                    "location": event.location or "",
                }
            )
        return sorted(events, key=lambda event: event["start"])

    def _free_windows(
        self,
        planning_input: PlanningInput,
        fixed_events: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        day_start = time(hour=7)
        day_end = time(hour=22)
        busy = [
            (self._parse_hhmm(event["start"]), self._parse_hhmm(event["end"]))
            for event in fixed_events
            if event.get("start") and event.get("end")
        ]
        busy.sort()
        windows = []
        cursor = day_start
        for start, end in busy:
            if start > cursor:
                windows.append({"start": cursor.strftime("%H:%M"), "end": start.strftime("%H:%M")})
            if end > cursor:
                cursor = end
        if cursor < day_end:
            windows.append({"start": cursor.strftime("%H:%M"), "end": day_end.strftime("%H:%M")})
        return windows

    @staticmethod
    def _suggest_tasks(tasks: list[str], free_windows: list[dict[str, str]]) -> list[dict[str, str]]:
        suggested = []
        for index, task in enumerate(tasks[:5]):
            window = free_windows[index % len(free_windows)] if free_windows else None
            suggested.append(
                {
                    "title": task,
                    "window": f"{window['start']}-{window['end']}" if window else "flexible",
                }
            )
        return suggested

    @staticmethod
    def _parse_hhmm(value: str) -> time:
        hour, minute = value.split(":", 1)
        return time(hour=int(hour), minute=int(minute))
