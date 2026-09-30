"""Validated execution and confirmation policy for the household assistant."""

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta

import httpx
from pydantic import ValidationError

from app.db.models import AssistantAction, CalendarProvider
from app.schemas.conversation import TOOL_MODELS
from app.services.calendar_service import CalendarService, CalendarSyncError
from app.services.finance_category_service import FinanceCategoryService

READ_TOOLS = {"list_records", "finance_query", "day_plan"}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def ids_in_result(value):
    if isinstance(value, dict):
        result = {value["id"]} if isinstance(value.get("id"), str) else set()
        for child in value.values():
            result |= ids_in_result(child)
        return result
    if isinstance(value, list):
        return set().union(*(ids_in_result(v) for v in value))
    return set()


class HouseholdTools:
    def __init__(self, data, repository, conversation, turn, known_ids):
        self.data = data
        self.repository = repository
        self.session = data.session
        self.conversation = conversation
        self.turn = turn
        self.known_ids = known_ids

    async def execute(self, name, raw, *, confirmed=False, expected=None):
        if name not in TOOL_MODELS or name == "escalate":
            return {"error": "Unsupported household tool."}
        try:
            args = TOOL_MODELS[name][0].model_validate(raw)
        except (ValidationError, ValueError):
            return {
                "error": (
                    "Invalid tool arguments. Ask for missing information or correct the fields."
                )
            }
        if name == "record_transaction" and args.category not in [
            *FinanceCategoryService.categories(),
            "Income",
        ]:
            return {
                "error": "Choose a supported category.",
                "categories": FinanceCategoryService.categories(),
            }
        if getattr(args, "record_id", None) and str(args.record_id) not in self.known_ids:
            return {"error": "Retrieve the matching records first; use an ID from the result."}
        try:
            if name in READ_TOOLS:
                result = await getattr(self.data, name)(args)
                self.known_ids |= ids_in_result(result)
                return result
            target = None
            if name == "change_record":
                target = self.data.record(
                    await self.data.get_record(args.kind, args.record_id, lock=True)
                )
            elif name == "calendar_change" and args.record_id:
                event = await self.data.calendar_record(args.record_id)
                if event.source_type != CalendarProvider.google:
                    return {
                        "error": (
                            "Only connected Google calendars support changes. iCloud and "
                            "iCal are read-only."
                        )
                    }
                target = {
                    "id": str(event.id),
                    "title": event.title,
                    "start": event.starts_at.isoformat(),
                    "end": event.ends_at.isoformat(),
                    "version": event.updated_at.isoformat(),
                }
            if confirmed and expected != fingerprint(target):
                return {
                    "error": (
                        "The selected record changed after the proposal. Please request "
                        "the change again."
                    )
                }
            requires_confirmation = name == "calendar_change" or (
                name == "change_record" and args.action == "remove"
            )
            if requires_confirmation and not confirmed:
                token = secrets.token_hex(4)
                if name == "change_record":
                    description = f"Remove {args.kind} item: {target['title']} (ID {target['id']})."
                else:
                    description = f"Calendar {args.action}: " + json.dumps(
                        {
                            "current": target,
                            "requested": args.model_dump(mode="json"),
                        },
                        ensure_ascii=False,
                    )
                self.conversation.pending = {
                    "tool": name,
                    "arguments": args.model_dump(mode="json"),
                    "fingerprint": fingerprint(target),
                    "token": token,
                    "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
                    "description": description,
                }
                await self.session.commit()
                return {
                    "confirmation": True,
                    "message": (
                        f"{description}\nReply confirm {token} to apply this change, "
                        f"or cancel. This expires in 10 minutes."
                    ),
                }
            action_key = fingerprint({"tool": name, "arguments": args.model_dump(mode="json")})
            prior = await self.repository.action(self.turn.id, action_key)
            if prior:
                return (
                    prior.result
                    if prior.status == "complete"
                    else {
                        "error": (
                            "This action has an uncertain outcome. Check the current data "
                            "before requesting it again."
                        )
                    }
                )
            action = AssistantAction(
                turn_id=self.turn.id, action_key=action_key, tool_name=name, result={}
            )
            self.session.add(action)
            if name == "calendar_change":
                # Record intent before the external request; never repeat an uncertain mutation.
                await self.session.commit()
                result = await self._calendar_change(args)
            else:
                result = await getattr(self.data, name)(args)
            action.result = result
            action.status = "complete"
            self.conversation.pending = None
            # DB mutations, audit records and action receipt commit together.
            await self.session.commit()
            self.known_ids |= ids_in_result(result)
            return result
        except ValueError as exc:
            await self._rollback()
            return {"error": str(exc)}
        except (httpx.HTTPError, CalendarSyncError, RuntimeError):
            await self._rollback()
            return {
                "error": (
                    "The calendar action could not be verified. Check Calendar "
                    "before retrying; I have not repeated it."
                )
            }

    async def _rollback(self):
        await self.session.rollback()
        await self.session.refresh(self.conversation)
        await self.session.refresh(self.turn)

    async def _calendar_change(self, args):
        service = CalendarService(self.session)
        if args.action == "create":
            await service.create_google_event(
                household_id=self.data.household_id,
                user_id=self.data.user_id,
                title=args.title,
                starts_at=args.starts_at,
                ends_at=args.ends_at,
                timezone=self.data.timezone,
            )
        else:
            event = await self.data.calendar_record(args.record_id)
            await service.change_household_event(
                event=event,
                household_id=self.data.household_id,
                action=args.action,
                title=args.title,
                starts_at=args.starts_at,
                ends_at=args.ends_at,
            )
        return {"message": f"Calendar {args.action} completed."}
