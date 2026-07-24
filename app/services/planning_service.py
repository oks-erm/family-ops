import re
from dataclasses import dataclass
from datetime import date, datetime, time
from uuid import UUID


@dataclass(frozen=True)
class CalendarEventInput:
    title: str
    starts_at: datetime
    ends_at: datetime
    location: str | None = None
    external_calendar_id: str | None = None
    external_event_id: str | None = None
    meeting_url: str | None = None
    conference_data: dict[str, object] | None = None


@dataclass(frozen=True)
class PlannedTaskInput:
    title: str
    duration_minutes: int | None = None
    must: bool = False
    preferred_window: str | None = None


@dataclass(frozen=True)
class PlanningInput:
    user_id: UUID
    plan_date: date
    work_start: time | None
    work_end: time | None
    unusual_notes: str | None
    tasks: list[str | PlannedTaskInput]
    calendar_events: list[CalendarEventInput]
    current_time: time | None = None
    available_start: time | None = None
    available_end: time | None = None


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
            "notes": self._display_notes(planning_input.unusual_notes),
            "fixed_events": fixed_events,
            "free_windows": free_windows,
            "tasks": self._serialize_tasks(planning_input.tasks),
            "suggested_tasks": self._suggest_tasks(planning_input.tasks, free_windows),
        }

    def render_plan_message(self, plan: dict[str, object], *, include_tasks: bool = True) -> str:
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
                if event.get("all_day"):
                    lines.append(f"- All day: {event['title']}")
                else:
                    lines.append(f"- {event['start']}-{event['end']}: {event['title']}")

        free_windows = plan.get("free_windows") or []
        if free_windows:
            lines.append("")
            lines.append("Free windows")
            for window in free_windows:
                lines.append(f"- {window['start']}-{window['end']}")

        tasks = plan.get("suggested_tasks") or []
        if include_tasks:
            if tasks:
                lines.append("")
                lines.append("Tasks")
                for task in tasks:
                    title = task["title"] if isinstance(task, dict) else str(task)
                    window = task.get("window") if isinstance(task, dict) else None
                    if window == "alternative":
                        lines.append(f"  or {title}")
                    else:
                        lines.append(f"- {title}" + (f" ({window})" if window and window != "flexible" else ""))
            elif not fixed_events and not plan.get("notes"):
                lines.append("")
                lines.append("Tasks")
                lines.append("- No pending tasks.")

        return "\n".join(lines)

    def render_morning_brief(self, plan: dict[str, object]) -> str:
        lines = ["Today"]
        work_block = plan.get("work_block") or {}
        if isinstance(work_block, dict) and (work_block.get("start") or work_block.get("end")):
            lines.append(f"Work: {work_block.get('start') or '?'}-{work_block.get('end') or '?'}")

        fixed_events = [
            event
            for event in (plan.get("fixed_events") or [])
            if isinstance(event, dict) and str(event.get("source") or "").lower() != "work"
        ]
        if fixed_events:
            event_labels = []
            for event in fixed_events[:4]:
                if event.get("all_day"):
                    event_labels.append(f"{event['title']} all day")
                else:
                    event_labels.append(f"{event['start']}-{event['end']} {event['title']}")
            if len(fixed_events) > 4:
                event_labels.append(f"+{len(fixed_events) - 4} more")
            lines.append("Busy: " + "; ".join(event_labels))

        free_windows = plan.get("free_windows") or []
        if free_windows:
            lines.append(
                "Free: "
                + ", ".join(f"{window['start']}-{window['end']}" for window in free_windows[:3])
            )

        tasks = plan.get("suggested_tasks") or []
        task_titles = []
        for task in tasks[:4]:
            if not isinstance(task, dict):
                task_titles.append(str(task))
                continue
            title = str(task.get("title") or "").strip()
            if title:
                task_titles.append(title)
        if task_titles:
            lines.append("Could do: " + ", ".join(task_titles))

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
        events.extend(self._fixed_events_from_notes(planning_input.unusual_notes))
        deduped = {}
        for event in events:
            key = (
                event["title"].strip().casefold(),
                event["start"],
                event["end"],
            )
            deduped[key] = event
        return sorted(deduped.values(), key=lambda event: event["start"])

    @classmethod
    def _display_notes(cls, notes: str | None) -> str | None:
        if not notes:
            return None
        display_parts = []
        for part in notes.split(";"):
            text = part.strip()
            if not text:
                continue
            if text.casefold() in {"today", "tonight", "tomorrow", "this week", "next week"}:
                continue
            if cls._fixed_events_from_notes(text):
                continue
            if cls._available_bounds_from_notes(text) != (None, None):
                continue
            display_parts.append(text)
        return "; ".join(display_parts) or None

    @staticmethod
    def _fixed_events_from_notes(notes: str | None) -> list[dict[str, str]]:
        if not notes:
            return []
        events = []
        for part in notes.split(";"):
            text = part.strip()
            if not text:
                continue
            all_day_match = re.search(r"\b(all day|birthday|anniversary)\b", text, flags=re.IGNORECASE)
            if all_day_match:
                title = re.sub(
                    r"\b(?:all day|for today|for tomorrow|today|tomorrow|tonight)\b",
                    "",
                    text,
                    flags=re.IGNORECASE,
                ).strip(" .,:-")
                if not title:
                    title = "All-day event"
                events.append(
                    {
                        "title": title[:80],
                        "start": "00:00",
                        "end": "23:59",
                        "source": "note",
                        "all_day": True,
                    }
                )
                continue
            match = re.search(
                r"(?P<title>.+?)(?:\s+(?:from|between))?\s+(?P<start>2[0-3]|[01]?\d)(?:(?::|\.|h)(?P<start_min>[0-5]\d))?\s*(?:to|and|-)\s*(?P<end>2[0-3]|[01]?\d)(?:(?::|\.|h)(?P<end_min>[0-5]\d))?",
                text,
                flags=re.IGNORECASE,
            )
            if not match:
                continue
            title = re.sub(
                r"^(?:change of plans[:,]?\s*)?(?:today|tomorrow|tonight)?\s*(?:is|there is|there's|i have|we have)?\s*",
                "",
                match.group("title").strip(" ."),
                flags=re.IGNORECASE,
            )
            title = re.sub(
                r"^(?:is|there is|there's|i have|we have|a|an)\s+",
                "",
                title,
                flags=re.IGNORECASE,
            ).strip(" .")
            if not title:
                title = "Fixed event"
            start = time(hour=int(match.group("start")), minute=int(match.group("start_min") or 0))
            end = time(hour=int(match.group("end")), minute=int(match.group("end_min") or 0))
            if start >= end:
                continue
            events.append(
                {
                    "title": title[:80],
                    "start": start.strftime("%H:%M"),
                    "end": end.strftime("%H:%M"),
                    "source": "note",
                }
            )
        return events

    def _free_windows(
        self,
        planning_input: PlanningInput,
        fixed_events: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        note_start, note_end = self._available_bounds_from_notes(planning_input.unusual_notes)
        day_start = planning_input.available_start or note_start or time(hour=8)
        if planning_input.current_time and planning_input.current_time > day_start:
            day_start = planning_input.current_time
        day_end = planning_input.available_end or note_end or time(hour=23, minute=30)
        if day_start >= day_end:
            return []
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
    def _suggest_tasks(
        tasks: list[str | PlannedTaskInput],
        free_windows: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        suggested = []
        windows = [
            {
                "start": PlanningService._parse_hhmm(window["start"]),
                "end": PlanningService._parse_hhmm(window["end"]),
            }
            for window in free_windows
        ]
        sorted_tasks = sorted(
            tasks,
            key=lambda task: 0 if isinstance(task, PlannedTaskInput) and task.must else 1,
        )
        previous_category = None
        for task in sorted_tasks[:8]:
            title, minutes = PlanningService._task_duration(task)
            category = PlanningService._task_category(title)
            preferred_window = PlanningService._preferred_window(task, title)
            if previous_category == category and category in {"physical", "errand"}:
                suggested.append(
                    {
                        "title": title,
                        "window": "alternative",
                    }
                )
                continue
            slot = PlanningService._reserve_first_slot_that_fits(
                windows,
                minutes,
                preferred_window=preferred_window,
            )
            suggested.append(
                {
                    "title": title,
                    "window": f"{slot[0].strftime('%H:%M')}-{slot[1].strftime('%H:%M')}" if slot else "flexible",
                }
            )
            previous_category = category
        return suggested

    @staticmethod
    def _preferred_window(task: str | PlannedTaskInput, title: str) -> str | None:
        if isinstance(task, PlannedTaskInput) and task.preferred_window:
            return task.preferred_window
        lowered = title.lower()
        if re.search(r"\b(morning|manhã|manha)\b", lowered):
            return "morning"
        if re.search(r"\b(afternoon|tarde)\b", lowered):
            return "afternoon"
        if re.search(r"\b(evening|tonight|noite)\b", lowered):
            return "evening"
        return None

    @staticmethod
    def _task_duration(task: str | PlannedTaskInput) -> tuple[str, int]:
        if isinstance(task, PlannedTaskInput):
            return task.title, task.duration_minutes or PlanningService._logical_duration(task.title)
        match = re.search(r"\((\d+)\s*min\)$", task)
        if match:
            return task[: match.start()].strip(), int(match.group(1))
        return task, PlanningService._logical_duration(task)

    @staticmethod
    def _serialize_tasks(tasks: list[str | PlannedTaskInput]) -> list[dict[str, object]]:
        serialized = []
        for task in tasks:
            if isinstance(task, PlannedTaskInput):
                serialized.append(
                    {
                        "title": task.title,
                        "duration_minutes": task.duration_minutes,
                        "must": task.must,
                    }
                )
            else:
                title, minutes = PlanningService._task_duration(task)
                serialized.append(
                    {
                        "title": title,
                        "duration_minutes": minutes,
                        "must": False,
                    }
                )
        return serialized

    @staticmethod
    def _logical_duration(task: str) -> int:
        lowered = task.lower()
        if "exercise" in lowered:
            return 25
        if "read" in lowered:
            return 15
        if "cook" in lowered:
            return 45
        if "clean" in lowered:
            return 30
        return 30

    @staticmethod
    def _task_category(task: str) -> str:
        lowered = task.lower()
        if any(word in lowered for word in ("exercise", "workout", "climb", "run", "gym", "walk")):
            return "physical"
        if any(word in lowered for word in ("shop", "buy", "pick up", "drop off", "errand")):
            return "errand"
        if any(word in lowered for word in ("read", "study", "write", "call", "email")):
            return "quiet"
        return "general"

    @staticmethod
    def _reserve_first_slot_that_fits(
        windows: list[dict[str, time]],
        minutes: int,
        preferred_window: str | None = None,
    ) -> tuple[time, time] | None:
        if preferred_window:
            for window in windows:
                start = window["start"]
                end = window["end"]
                if not PlanningService._window_matches_preference(start=start, end=end, preference=preferred_window):
                    continue
                if PlanningService._minutes_between(start, end) >= minutes:
                    slot_end = PlanningService._add_minutes(start, minutes)
                    window["start"] = slot_end
                    return start, slot_end
        for window in windows:
            start = window["start"]
            end = window["end"]
            if PlanningService._minutes_between(start, end) >= minutes:
                slot_end = PlanningService._add_minutes(start, minutes)
                window["start"] = slot_end
                return start, slot_end
        return None

    @staticmethod
    def _window_matches_preference(*, start: time, end: time, preference: str) -> bool:
        start_minutes = start.hour * 60 + start.minute
        end_minutes = end.hour * 60 + end.minute
        if preference == "morning":
            return start_minutes < 12 * 60 and end_minutes > 5 * 60
        if preference == "afternoon":
            return start_minutes < 18 * 60 and end_minutes > 12 * 60
        if preference == "evening":
            return end_minutes > 18 * 60
        return True

    @staticmethod
    def _parse_hhmm(value: str) -> time:
        hour, minute = value.split(":", 1)
        return time(hour=int(hour), minute=int(minute))

    @staticmethod
    def _available_bounds_from_notes(notes: str | None) -> tuple[time | None, time | None]:
        if not notes:
            return None, None
        lowered = notes.lower()
        wake = None
        sleep = None
        for match in re.finditer(
            r"\b(?P<kind>wake(?:\s+up)?|sleep|go\s+to\s+sleep|bed)\b(?:\s+at)?\s+(?P<hour>2[0-3]|[01]?\d)(?:(?::|\.|h)(?P<minute>[0-5]\d))?\b",
            lowered,
        ):
            parsed = time(hour=int(match.group("hour")), minute=int(match.group("minute") or 0))
            kind = match.group("kind")
            if "wake" in kind:
                wake = parsed
            else:
                sleep = parsed
        return wake, sleep

    @staticmethod
    def _minutes_between(start: time, end: time) -> int:
        return (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)

    @staticmethod
    def _add_minutes(value: time, minutes: int) -> time:
        total = value.hour * 60 + value.minute + minutes
        return time(hour=min(total // 60, 23), minute=total % 60)
