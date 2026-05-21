import re
import unicodedata


class GroceryNormalizer:
    _phrase_replacements = {
        "avo": "avocado",
        "avos": "avocado",
        "avocadoes": "avocado",
        "abacate": "avocado",
        "abacates": "avocado",
        "brocolos": "broccoli",
        "brócolos": "broccoli",
        "brocolo": "broccoli",
        "brócolo": "broccoli",
        "leite": "milk",
        "ovos": "egg",
        "ovo": "egg",
        "arroz": "rice",
        "massa": "pasta",
        "pao": "bread",
        "pão": "bread",
        "queijo": "cheese",
        "manteiga": "butter",
        "frango": "chicken",
        "peixe": "fish",
        "batata": "potato",
        "batatas": "potato",
        "tomate": "tomato",
        "tomates": "tomato",
        "tomatoes": "tomato",
        "cebola": "onion",
        "cebolas": "onion",
        "alho": "garlic",
        "alhos": "garlic",
        "banana": "banana",
        "bananas": "banana",
        "maca": "apple",
        "maça": "apple",
        "maçã": "apple",
        "macas": "apple",
        "maças": "apple",
        "maçãs": "apple",
        "iogurte": "yogurt",
        "iogurtes": "yogurt",
        "champô": "shampoo",
        "champo": "shampoo",
        "detergente": "detergent",
        "guardanapos": "napkin",
        "guardanapo": "napkin",
        "molho tomate": "tomato sauce",
        "molho de tomate": "tomato sauce",
        "papel higienico": "toilet paper",
        "papel higiénico": "toilet paper",
    }

    _drop_words = {
        "organic",
        "bio",
        "fresh",
        "fresco",
        "fresca",
        "frescos",
        "frescas",
        "pack",
        "pkg",
        "maduro",
        "madura",
        "maduros",
        "maduras",
        "maturado",
        "maturada",
        "maturados",
        "maturadas",
        "un",
        "unid",
        "unidade",
        "unidades",
    }

    def normalize(self, value: str) -> str:
        text = self._strip_accents(value.lower())
        text = re.sub(r"[^a-z0-9 ]+", " ", text)
        text = re.sub(r"\b\d+(?:g|kg|ml|l|x)?\b", " ", text)
        text = " ".join(text.split())

        if text in self._phrase_replacements:
            text = self._phrase_replacements[text]
        for phrase, replacement in self._phrase_replacements.items():
            if " " in phrase and phrase in text:
                text = text.replace(phrase, replacement)

        words = []
        for word in text.split():
            word = self._phrase_replacements.get(word, word)
            if word in self._drop_words:
                continue
            words.append(self._singularize(word))
        return " ".join(words)

    def tokens(self, value: str) -> set[str]:
        return set(self.normalize(value).split())

    @staticmethod
    def _strip_accents(value: str) -> str:
        normalized = unicodedata.normalize("NFKD", value)
        return "".join(char for char in normalized if not unicodedata.combining(char))

    @staticmethod
    def _singularize(word: str) -> str:
        if word.endswith("ies") and len(word) > 4:
            return word[:-3] + "y"
        if word.endswith("atoes") and len(word) > 5:
            return word[:-2]
        if word.endswith("oes") and len(word) > 4:
            return word[:-3]
        if word.endswith("es") and len(word) > 4:
            return word[:-2]
        if word.endswith("s") and len(word) > 3:
            return word[:-1]
        return word
