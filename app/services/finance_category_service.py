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
        for category, terms in self._expense_terms().items():
            if tokens & terms or any(term in normalized for term in terms if " " in term):
                return category
        return "Other"

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
            "Taxes": {"tax", "taxes", "irs", "imposto", "impostos", "financas", "finanças"},
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
