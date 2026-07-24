import unittest

from app.services.finance_category_service import FinanceCategoryService


class FinanceCategoryServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = FinanceCategoryService()

    def test_includes_church_and_tech_categories(self) -> None:
        categories = self.service.categories()

        self.assertIn("Church", categories)
        self.assertIn("Tech & Devices", categories)

    def test_classifies_church_expenses(self) -> None:
        self.assertEqual(self.service.category_for("church donation 20"), "Church")
        self.assertEqual(self.service.category_for("dízimo igreja 50"), "Church")

    def test_classifies_tech_device_expenses(self) -> None:
        self.assertEqual(self.service.category_for("Worten charger 24.99"), "Tech & Devices")
        self.assertEqual(self.service.category_for("iPhone cable"), "Tech & Devices")


if __name__ == "__main__":
    unittest.main()
