from app.services.grocery_normalizer import GroceryNormalizer


class FinanceCategoryService:
    def __init__(self) -> None:
        self.normalizer = GroceryNormalizer()

    def category_for(self, text: str, *, is_income: bool = False) -> str:
        normalized = self.normalizer.normalize(text)
        tokens = set(normalized.split())
        if is_income:
            if tokens & {"salary", "wage", "pay", "paid", "income", "salario", "ordenado"}:
                return "Income"
            return "Income"
        merchant_category = self._merchant_category(normalized)
        if merchant_category is not None:
            return merchant_category
        for category, terms in self._expense_terms().items():
            if tokens & terms or any(term in normalized for term in terms if " " in term):
                return category
        return "Other"

    @staticmethod
    def categories() -> list[str]:
        return [
            "Food",
            "Eat Out",
            "Commute",
            "Sport",
            "Entertainment",
            "Health",
            "Beauty",
            "House Chemicals",
            "Subscriptions",
            "PayPal",
            "Taxes",
            "Utilities",
            "Other",
        ]

    @staticmethod
    def _merchant_category(normalized: str) -> str | None:
        merchant_rules = {
            "minimi": "Food",
            "mini mi": "Food",
            "mini mercado": "Food",
            "heroku": "Subscriptions",
            "paypal": "PayPal",
            "fidelidade": "Health",
            "the pra": "Health",
        }
        for marker, category in merchant_rules.items():
            if marker in normalized:
                return category
        return None

    @staticmethod
    def _expense_terms() -> dict[str, set[str]]:
        return {
            "Food": {
                "aldi",
                "lidl",
                "continente",
                "pingo",
                "doce",
                "auchan",
                "mercadona",
                "grocery",
                "supermarket",
                "supermercado",
                "food",
                "groceries",
            },
            "Eat Out": {
                "eat",
                "restaurant",
                "restaurante",
                "cafe",
                "takeaway",
                "takeout",
                "delivery",
                "uber eats",
                "glovo",
                "bolt food",
                "mcdonalds",
                "burger",
                "pizza",
            },
            "Commute": {
                "petrol",
                "gas",
                "fuel",
                "gasolina",
                "diesel",
                "galp",
                "repsol",
                "bp",
                "uber",
                "bolt",
                "metro",
                "train",
                "bus",
                "taxi",
                "transport",
            },
            "Sport": {"gym", "sport", "fitness", "decathlon", "yoga", "padel", "swim"},
            "Entertainment": {
                "cinema",
                "movie",
                "netflix",
                "spotify",
                "game",
                "concert",
                "theatre",
                "teatro",
                "entertainment",
            },
            "Health": {
                "pharmacy",
                "farmacia",
                "doctor",
                "clinic",
                "hospital",
                "dentist",
                "medicine",
                "health",
            },
            "Beauty": {
                "beauty",
                "hair",
                "nails",
                "cosmetic",
                "sephora",
                "perfume",
                "barber",
                "salon",
            },
            "House Chemicals": {
                "detergent",
                "cleaner",
                "bleach",
                "sponge",
                "house",
                "cleaning",
                "detergente",
                "lixivia",
            },
            "Taxes": {"tax", "taxes", "irs", "iva", "vat", "imposto", "impostos", "financas", "finanças"},
            "Utilities": {
                "electricity",
                "water",
                "internet",
                "phone",
                "vodafone",
                "meo",
                "nos",
                "edp",
                "utility",
            },
        }
