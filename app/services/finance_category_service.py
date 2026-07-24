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
            "Uber",
            "Gas",
            "Tolls",
            "Public Transport",
            "Sport",
            "Entertainment",
            "Church",
            "Health",
            "Beauty",
            "Tech & Devices",
            "House Chemicals",
            "Subscriptions",
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
            # PayPal transactions are typically Uber rides
            "paypal": "Uber",
            "fidelidade": "Health",
            "the pra": "Health",
            "apple": "Tech & Devices",
            "worten": "Tech & Devices",
            "fnac": "Tech & Devices",
            "radio popular": "Tech & Devices",
            "pc diga": "Tech & Devices",
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
            "Uber": {
                "uber",
                "bolt",
                "taxi",
                "cabify",
                "ride",
            },
            "Gas": {
                "petrol",
                "gas",
                "fuel",
                "gasolina",
                "diesel",
                "galp",
                "repsol",
                "bp",
                "cepsa",
                "prio",
                "posto",
                "estacao",
                "est servic",
                "veiga seabra",
                "veiga e seabra",
            },
            "Tolls": {
                "toll",
                "tolls",
                "portagem",
                "portagens",
                "via verde",
                "viaverde",
                "autopista",
                "autoestrada",
            },
            "Public Transport": {
                "metro",
                "train",
                "bus",
                "transport",
                "carris",
                "stcp",
                "comboio",
                "cp ",
                "fertagus",
                "tram",
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
            "Church": {
                "church",
                "igreja",
                "dizimo",
                "dízimo",
                "tithe",
                "offering",
                "oferta",
                "donation",
                "doacao",
                "doação",
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
            "Tech & Devices": {
                "tech",
                "device",
                "devices",
                "electronics",
                "electronica",
                "eletronica",
                "electrónica",
                "eletrodomestico",
                "eletrodoméstico",
                "computer",
                "laptop",
                "iphone",
                "smartphone",
                "android",
                "ipad",
                "tablet",
                "charger",
                "cable",
                "headphones",
                "software",
                "hardware",
                "apple",
                "worten",
                "fnac",
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
            "Taxes": {
                "tax",
                "taxes",
                "irs",
                "iva",
                "vat",
                "imposto",
                "impostos",
                "financas",
                "finanças",
            },
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
