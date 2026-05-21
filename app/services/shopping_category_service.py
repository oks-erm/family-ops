from app.services.grocery_normalizer import GroceryNormalizer


class ShoppingCategoryService:
    def __init__(self) -> None:
        self.normalizer = GroceryNormalizer()

    def category_for(self, item_name: str) -> str:
        normalized = self.normalizer.normalize(item_name)
        tokens = set(normalized.split())

        if tokens & self._cosmetics():
            return "Cosmetics"
        if tokens & self._clothes():
            return "Clothes"
        if tokens & self._house():
            return "House"
        return "Food"

    @staticmethod
    def _cosmetics() -> set[str]:
        return {
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
    def _clothes() -> set[str]:
        return {
            "shirt",
            "tshirt",
            "t",
            "jeans",
            "pants",
            "socks",
            "underwear",
            "bra",
            "dress",
            "shoes",
            "coat",
            "jacket",
            "camisa",
            "calca",
            "calcas",
            "meia",
            "meias",
            "sapato",
            "sapatos",
            "casaco",
            "vestido",
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
            "battery",
            "bulb",
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
