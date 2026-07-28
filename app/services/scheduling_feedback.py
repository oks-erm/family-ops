import asyncio
import re
import smtplib
import ssl
from email.message import EmailMessage
from urllib.parse import urlsplit

import httpx

from app.config import Settings


class FeedbackConfigurationError(RuntimeError):
    pass


class FeedbackDeliveryError(RuntimeError):
    pass


class TurnstileValidationError(ValueError):
    pass


def normalize_feedback_text(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    normalized = "".join(
        character
        for character in normalized
        if character in {"\n", "\t"} or ord(character) >= 32
    )
    return re.sub(r"\n{4,}", "\n\n\n", normalized).strip()


class SchedulingFeedbackService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def configured(self) -> bool:
        return all(
            (
                self.settings.scheduling_feedback_smtp_username,
                self.settings.scheduling_feedback_smtp_app_password,
                self.settings.scheduling_feedback_to_email,
                self.settings.turnstile_site_key,
                self.settings.turnstile_secret_key,
            )
        )

    async def verify_turnstile(
        self,
        *,
        token: str,
        remote_ip: str | None,
        expected_hostname: str | None,
    ) -> None:
        secret = self.settings.turnstile_secret_key
        if not secret:
            raise FeedbackConfigurationError("Turnstile is not configured.")
        data = {"secret": secret, "response": token}
        if remote_ip:
            data["remoteip"] = remote_ip
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.post(
                    "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                    data=data,
                )
                response.raise_for_status()
                result = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise TurnstileValidationError(
                "The security check could not be verified. Please try again."
            ) from exc
        if not result.get("success") or result.get("action") != "bug-report":
            raise TurnstileValidationError(
                "The security check expired or was unsuccessful. Please try again."
            )
        hostname = str(result.get("hostname") or "").casefold()
        if expected_hostname and hostname != expected_hostname.casefold():
            raise TurnstileValidationError("The security check was issued for another website.")

    async def send_bug_report(
        self,
        *,
        reporter_email: str,
        section: str,
        message: str,
    ) -> None:
        username = self.settings.scheduling_feedback_smtp_username
        app_password = self.settings.scheduling_feedback_smtp_app_password
        recipient = self.settings.scheduling_feedback_to_email
        if not username or not app_password or not recipient:
            raise FeedbackConfigurationError("Feedback email is not configured.")

        clean_message = normalize_feedback_text(message)
        email = EmailMessage()
        email["Subject"] = "[Tutor scheduling] Bug report"
        email["From"] = username
        email["To"] = recipient
        email["Reply-To"] = reporter_email
        email.set_content(
            "A signed-in tutor submitted a bug report.\n\n"
            f"Reporter: {reporter_email}\n"
            f"Dashboard section: {section}\n\n"
            f"Report:\n{clean_message}\n"
        )
        try:
            await asyncio.to_thread(self._send_message, email, username, app_password)
        except (OSError, smtplib.SMTPException) as exc:
            raise FeedbackDeliveryError("The bug report could not be delivered.") from exc

    @staticmethod
    def _send_message(
        message: EmailMessage,
        username: str,
        app_password: str,
    ) -> None:
        with smtplib.SMTP_SSL(
            "smtp.gmail.com",
            465,
            timeout=10,
            context=ssl.create_default_context(),
        ) as smtp:
            smtp.login(username, app_password)
            smtp.send_message(message)


def scheduling_hostname(settings: Settings) -> str | None:
    return urlsplit(settings.scheduling_public_base_url or settings.public_base_url).hostname
