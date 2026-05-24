from app.services.grocery_normalizer import GroceryNormalizer


class ShoppingCategoryService:
    def __init__(self) -> None:
        self.normalizer = GroceryNormalizer()

    def category_for(self, item_name: str, *, store_name_raw: str | None = None) -> str:
        if store_name_raw and store_name_raw.strip().lower() in {"online", "internet", "amazon"}:
            return "Online"
        normalized = self.normalizer.normalize(item_name)
        tokens = set(normalized.split())

        if tokens & self._health():
            return "Health"
        if tokens & self._tech():
            return "Tech"
        if tokens & self._clothes():
            return "Clothes"
        if tokens & self._house():
            return "House"
        return "Supermarket"

    @staticmethod
    def _health() -> set[str]:
        return {
            # medications & pharmacy
            "paracetamol",
            "ibuprofen",
            "aspirin",
            "antibiotic",
            "antibiotic",
            "medicine",
            "medication",
            "pill",
            "pills",
            "tablet",
            "tablets",
            "capsule",
            "capsules",
            "pharmacy",
            "farmacia",
            "farmácia",
            "medicamento",
            "medicamentos",
            "remedio",
            "remédio",
            "comprimido",
            "comprimidos",
            # supplements / vitamins
            "vitamin",
            "vitamins",
            "vitamina",
            "vitaminas",
            "supplement",
            "supplements",
            "suplemento",
            "suplementos",
            "magnesium",
            "magnesio",
            "zinc",
            "zink",
            "omega",
            "probiotic",
            "probiotico",
            "collagen",
            "colagénio",
            "melatonin",
            "melatonina",
            "iron",
            "ferro",
            "calcium",
            "calcio",
            "cálcio",
            "biotin",
            "biotina",
            # cosmetics / personal care (kept here as "Health & Beauty")
            "shampoo",
            "conditioner",
            "soap",
            "gel",
            "toothpaste",
            "toothbrush",
            "deodorant",
            "cream",
            "lotion",
            "makeup",
            "mascara",
            "cosmetic",
            "champo",
            "sabonete",
            "pasta",
            "dente",
            "desodorizante",
            "creme",
        }

    @staticmethod
    def _tech() -> set[str]:
        return {
            "laptop",
            "computer",
            "phone",
            "smartphone",
            "tablet",
            "cable",
            "charger",
            "carregador",
            "headphones",
            "headphone",
            "earphones",
            "earphone",
            "speaker",
            "keyboard",
            "mouse",
            "monitor",
            "screen",
            "printer",
            "router",
            "usb",
            "hdmi",
            "adapter",
            "adaptador",
            "powerbank",
            "power",
            "battery",
            "bateria",
            "smartwatch",
            "watch",
            "camera",
            "camara",
        }

    @staticmethod
    def _clothes() -> set[str]:
        return {
            "shirt",
            "tshirt",
            "jeans",
            "pants",
            "socks",
            "underwear",
            "bra",
            "dress",
            "shoes",
            "coat",
            "jacket",
            "sneakers",
            "sandals",
            "boots",
            "camisa",
            "calca",
            "calcas",
            "meia",
            "meias",
            "sapato",
            "sapatos",
            "casaco",
            "vestido",
            "sapatilhas",
            "tenis",
            "ténis",
        }

    @staticmethod
    def _house() -> set[str]:
        return {
            "detergent",
            "cleaner",
            "sponge",
            "napkin",
            "paper",
            "towel",
            "toilet",
            "bag",
            "trash",
            "foil",
            "detergente",
            "esponja",
            "guardanapo",
            "papel",
            "lixo",
            "pilha",
            "lampada",
            "lâmpada",
        }
