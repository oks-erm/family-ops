import ast
import unittest
from pathlib import Path

import yaml


class SeparationTests(unittest.TestCase):
    def test_production_release_enables_verified_economical_models(self):
        env = yaml.safe_load(Path("docker-compose.prod.yml").read_text())["services"]["app"][
            "environment"
        ]
        self.assertEqual(env["ASSISTANT_V2_ENABLED"], "true")
        self.assertEqual(env["ASSISTANT_MODEL"], "gpt-6-luna")
        self.assertEqual(env["ASSISTANT_REASONING_MODEL"], "gpt-6.1-sol")

    def test_family_deployment_has_no_scheduling_service(self):
        for name in ("docker-compose.yml", "docker-compose.prod.yml"):
            services = yaml.safe_load(Path(name).read_text())["services"]
            self.assertNotIn("tutor-scheduling", services)
        workflow = Path(".github/workflows/deploy.yml").read_text()
        self.assertIn("up -d --no-deps app", workflow)
        self.assertNotIn("--remove-orphans", workflow)
        self.assertIn('target: "/opt/family-copilot/household-release"', workflow)
        self.assertIn("cd /opt/family-copilot/household-release", workflow)
        self.assertIn("docker compose -p family-copilot", workflow)

    def test_image_excludes_scheduling_and_credentials(self):
        ignored = Path(".dockerignore").read_text().splitlines()
        for name in ("tutor-scheduling", ".env", ".env.*", ".git"):
            self.assertIn(name, ignored)

    def test_household_calendar_does_not_reference_tutor_models(self):
        source = Path("app/services/calendar_service.py").read_text()
        self.assertNotIn("SchedulingCalendar", source)
        self.assertNotIn("SchedulingProfile", source)
        self.assertIn("HouseholdCalendarSelection", source)

    def test_conversation_modules_cannot_import_scheduling(self):
        paths = list(Path("app/services/conversation").glob("*.py"))
        paths += list(Path("app/clients").glob("*.py"))
        paths += [
            Path("app/db/repositories/assistant_data.py"),
            Path("app/db/repositories/conversations.py"),
        ]
        for path in paths:
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    self.assertNotIn("scheduling", node.module or "", str(path))
                    for alias in node.names:
                        self.assertNotIn(
                            alias.name, {"SchedulingProfile", "LessonBooking", "StudentPayment"}
                        )

    def test_only_additive_household_migration(self):
        tree = ast.parse(
            Path("alembic/versions/202609300001_household_conversations.py").read_text()
        )
        upgrade = next(
            n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "upgrade"
        )
        calls = [
            n.func.attr
            for n in ast.walk(upgrade)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        ]
        self.assertNotIn("drop_table", calls)
        self.assertNotIn("drop_column", calls)
