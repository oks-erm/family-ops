from dataclasses import dataclass


@dataclass(frozen=True)
class ParsedShoppingItem:
    name: str
    store_name: str | None


class ShoppingService:
    def parse_add_items(self, text: str) -> list[ParsedShoppingItem]:
        parsed = self.parse_add_item(text)
        if parsed is None:
            return []
        item_names = self._split_items(parsed.name)
        if len(item_names) <= 1:
            return [parsed]
        return [
            ParsedShoppingItem(name=item_name, store_name=parsed.store_name)
            for item_name in item_names
        ]

    def parse_add_item(self, text: str) -> ParsedShoppingItem | None:
        normalized = text.strip()
        lowered = normalized.lower()
        for prefix in ("need to buy ", "i need to buy ", "we need to buy "):
            if lowered.startswith(prefix):
                remainder = normalized[len(prefix) :].strip()
                if not self._looks_like_shopping_item(remainder):
                    return None
                return self._parse_item_remainder(remainder)
        for blocked_prefix in ("need to ", "i need to ", "we need to ", "preciso de fazer ", "precisamos de fazer "):
            if lowered.startswith(blocked_prefix):
                return None
        for prefix in (
            "need ",
            "i need ",
            "we need ",
            "buy ",
            "add to shopping list ",
            "add shopping ",
            "preciso de ",
            "precisamos de ",
            "comprar ",
        ):
            if lowered.startswith(prefix):
                remainder = normalized[len(prefix) :].strip()
                if not self._looks_like_shopping_item(remainder):
                    return None
                return self._parse_item_remainder(remainder)
        return None

    def _parse_item_remainder(self, remainder: str) -> ParsedShoppingItem:
        remainder_lower = remainder.lower()
        store_separator = self._store_separator(remainder_lower)
        if store_separator is not None:
            split_at, separator = store_separator
            item = remainder[:split_at]
            store = remainder[split_at + len(separator) :]
            if store.strip().lower() in {"anywhere", "qualquer sitio", "qualquer sítio", "qualquer loja"}:
                return ParsedShoppingItem(name=item.strip(), store_name=None)
            return ParsedShoppingItem(name=item.strip(), store_name=store.strip())
        anywhere_suffix = self._anywhere_suffix(remainder_lower)
        if anywhere_suffix is not None:
            return ParsedShoppingItem(name=remainder[: -len(anywhere_suffix)].strip(), store_name=None)
        trailing_store = self._trailing_store(remainder)
        if trailing_store is not None:
            item_name, store_name = trailing_store
            return ParsedShoppingItem(name=item_name, store_name=store_name)
        return ParsedShoppingItem(name=remainder, store_name=None)

    def parse_store_visit(self, text: str) -> str | None:
        normalized = text.strip()
        lowered = normalized.lower()
        prefixes = ("going to ", "i am going to ", "i'm going to ", "vou ao ", "vou a ", "vou para ")
        for prefix in prefixes:
            if lowered.startswith(prefix):
                store_name = normalized[len(prefix) :].strip()
                return store_name or None
        return None

    def parse_purchased_items(self, text: str) -> list[str]:
        normalized = text.strip()
        lowered = normalized.lower()
        prefixes = (
            "got ",
            "bought ",
            "picked up ",
            "i got ",
            "i bought ",
            "we got ",
            "we bought ",
            "comprei ",
            "compramos ",
            "apanhei ",
            "apanhamos ",
            "ja comprei ",
            "já comprei ",
        )
        for prefix in prefixes:
            if lowered.startswith(prefix):
                remainder = normalized[len(prefix) :].strip()
                return self._split_purchased_items(remainder)
        return []

    def _split_items(self, text: str) -> list[str]:
        """Split on commas only — 'and'/'e' in product names (e.g. 'salt and pepper') must not be split."""
        return [item.strip(" .") for item in text.split(",") if item.strip(" .")]

    def _split_purchased_items(self, text: str) -> list[str]:
        """Split purchased items list; 'and'/'e' treated as list separators here."""
        cleaned = text.replace(" and ", ",").replace(" e ", ",")
        return [item.strip(" .") for item in cleaned.split(",") if item.strip(" .")]

    @staticmethod
    def _store_separator(lowered_text: str) -> tuple[int, str] | None:
        for separator in (" from ", " de ", " do ", " da ", " no ", " na "):
            if separator in lowered_text:
                return lowered_text.rfind(separator), separator
        return None

    @staticmethod
    def _anywhere_suffix(lowered_text: str) -> str | None:
        for suffix in (" anywhere", " qualquer sitio", " qualquer sítio", " qualquer loja"):
            if lowered_text.endswith(suffix):
                return suffix
        return None

    @classmethod
    def _trailing_store(cls, text: str) -> tuple[str, str] | None:
        cleaned = text.strip(" .")
        lowered = cleaned.lower()
        for store in cls._known_stores():
            suffix = f" {store}"
            if lowered.endswith(suffix):
                item_name = cleaned[: -len(suffix)].strip(" .")
                if item_name:
                    return item_name, cls._display_store_name(store)
        return None

    @staticmethod
    def _known_stores() -> tuple[str, ...]:
        return (
            "lidl",
            "aldi",
            "continente",
            "pingo doce",
            "auchan",
            "mercadona",
            "minipreco",
            "minipreço",
            "intermarche",
            "intermarché",
        )

    @staticmethod
    def _display_store_name(store: str) -> str:
        names = {
            "lidl": "Lidl",
            "aldi": "Aldi",
            "continente": "Continente",
            "pingo doce": "Pingo Doce",
            "auchan": "Auchan",
            "mercadona": "Mercadona",
            "minipreco": "Minipreco",
            "minipreço": "Minipreço",
            "intermarche": "Intermarche",
            "intermarché": "Intermarché",
        }
        return names.get(store, store.title())

    @classmethod
    def _looks_like_shopping_item(cls, text: str) -> bool:
        lowered = text.lower().strip()
        if any(action in lowered for action in cls._action_markers()):
            return False
        return True

    @staticmethod
    def _action_markers() -> tuple[str, ...]:
        return (
            "cook ",
            "make ",
            "prepare ",
            "clean ",
            "call ",
            "email ",
            "book ",
            "pay ",
            "go to ",
            "workout",
            "exercise",
            "finish ",
            "fix ",
            "organize ",
            "wash ",
        )

    @staticmethod
    def _grocery_tokens() -> tuple[str, ...]:
        return (
            "egg",
            "milk",
            "bread",
            "rice",
            "pasta",
            "sauce",
            "tomato",
            "broccoli",
            "avocado",
            "lentil",
            "bean",
            "cheese",
            "yogurt",
            "butter",
            "chicken",
            "beef",
            "fish",
            "salmon",
            "tuna",
            "ham",
            "fruit",
            "apple",
            "banana",
            "orange",
            "vegetable",
            "potato",
            "onion",
            "garlic",
            "carrot",
            "nuts",
            "cashew",
            "walnut",
            "oat",
            "water",
            "tonic",
            "coffee",
            "tea",
            "shampoo",
            "soap",
            "toothpaste",
            "detergent",
            "cleaner",
            "paper",
            "caju",
            "noz",
            "nozes",
            "aveia",
            "queijo",
            "ovos",
            "leite",
            "arroz",
            "massa",
        )
