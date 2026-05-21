import re
from dataclasses import dataclass
from html import unescape
from urllib.parse import quote_plus
from uuid import UUID

import asyncio
import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.prices import PriceRepository
from app.db.repositories.receipts import ReceiptRepository
from app.db.repositories.shopping import ShoppingRepository


@dataclass(frozen=True)
class PriceScanItem:
    name: str
    shopping_item_id: UUID | None
    store_name: str | None = None


class PriceService:
    STORE_SEARCH_URLS = {
        "continente": "https://www.continente.pt/pesquisa/?q={query}",
        "lidl": "https://www.lidl.pt/q/search?q={query}",
        "aldi": "https://www.aldi.pt/search.html?search={query}",
        "pingo doce": "https://www.pingodoce.pt/?s={query}",
        "mercadona": "https://www.mercadona.pt/pt/search-results/?search={query}",
        "minimix": "https://myminimix.pt/?s={query}",
    }
    STORE_DISPLAY_NAMES = {
        "continente": "Continente",
        "lidl": "Lidl",
        "aldi": "Aldi",
        "pingo doce": "Pingo Doce",
        "mercadona": "Mercadona",
        "minimix": "MiniMix",
    }
    MAIN_STORES = tuple(STORE_SEARCH_URLS)
    MAX_HISTORY_ITEMS = 30
    MAX_CONCURRENT_REQUESTS = 8
    QUERY_ALIASES = {
        "cashew": ("caju",),
        "cashews": ("caju",),
        "walnut": ("miolo de noz", "nozes", "noz"),
        "walnuts": ("miolo de noz", "nozes", "noz"),
        "oat": ("flocos de aveia", "aveia"),
        "oats": ("flocos de aveia", "aveia"),
        "tonic water": ("agua tonica", "água tónica"),
        "cottage cheese": ("queijo cottage", "requeijao", "requeijão"),
    }
    FALLBACK_PRODUCT_URLS = {
        "lidl": {
            "caju": (
                "https://www.lidl.pt/p/caju-torrado-sem-sal/p10022985",
                "https://www.lidl.pt/p/alesto-selection-caju-ao-natural/p10032546",
            ),
            "cashew": (
                "https://www.lidl.pt/p/caju-torrado-sem-sal/p10022985",
                "https://www.lidl.pt/p/alesto-selection-caju-ao-natural/p10032546",
            ),
            "cashews": (
                "https://www.lidl.pt/p/caju-torrado-sem-sal/p10022985",
                "https://www.lidl.pt/p/alesto-selection-caju-ao-natural/p10032546",
            ),
        }
    }

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.shopping_repository = ShoppingRepository(session)
        self.receipt_repository = ReceiptRepository(session)
        self.price_repository = PriceRepository(session)

    async def refresh_shopping_prices(self, *, household_id: UUID) -> int:
        items = await self._scan_items(household_id=household_id)
        semaphore = asyncio.Semaphore(self.MAX_CONCURRENT_REQUESTS)
        async with httpx.AsyncClient(
            timeout=8,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 FamilyCopilot/0.1"},
        ) as client:
            tasks = [
                self._fetch_with_limit(
                    semaphore=semaphore,
                    client=client,
                    store=store,
                    item=item,
                )
                for item in items
                for store in self._stores_for_item(item)
            ]
            results = await asyncio.gather(*tasks)

        count = 0
        for item, store, quote in results:
            if quote is None:
                continue
            await self.price_repository.add_quote(
                household_id=household_id,
                shopping_item_id=item.shopping_item_id,
                item_name=item.name,
                store_name=self.STORE_DISPLAY_NAMES.get(store, store.title()),
                product_name=quote["product_name"],
                price=quote["price"],
                old_price=quote["old_price"],
                product_url=quote["url"],
                is_promotion=bool(quote["old_price"]),
                commit=False,
            )
            count += 1
        if count:
            await self.session.commit()
        return count

    async def _scan_items(self, *, household_id: UUID) -> list[PriceScanItem]:
        pending_items = await self.shopping_repository.list_all_pending_for_household(household_id=household_id)
        scan_items = [
            PriceScanItem(name=item.name, shopping_item_id=item.id, store_name=item.store_name_raw)
            for item in pending_items
            if item.name.strip()
        ]
        seen = {self._normalize_item_name(item.name) for item in scan_items}

        receipts = await self.receipt_repository.list_receipts_for_household(household_id=household_id)
        for receipt in receipts:
            for receipt_item in receipt.items:
                name = receipt_item.name.strip()
                normalized = self._normalize_item_name(name)
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                scan_items.append(PriceScanItem(name=name, shopping_item_id=None, store_name=None))
                if len(scan_items) >= self.MAX_HISTORY_ITEMS + len(pending_items):
                    return scan_items
        return scan_items

    async def _fetch_with_limit(
        self,
        *,
        semaphore: asyncio.Semaphore,
        client: httpx.AsyncClient,
        store: str,
        item: PriceScanItem,
    ) -> tuple[PriceScanItem, str, dict[str, str | None] | None]:
        async with semaphore:
            quote = await self._fetch_store_quote(client=client, store=store, query=item.name)
        return item, store, quote

    async def _fetch_store_quote(
        self,
        *,
        client: httpx.AsyncClient,
        store: str,
        query: str,
    ) -> dict[str, str | None] | None:
        url_template = self.STORE_SEARCH_URLS.get(store.lower())
        if url_template is None:
            return None
        for query_variant in self._query_variants(query):
            url = url_template.format(query=quote_plus(query_variant))
            try:
                response = await client.get(url)
                response.raise_for_status()
            except Exception:
                continue

            quote = self._extract_store_quote(
                store=store,
                html=response.text,
                query=query_variant,
                url=str(response.url),
            )
            if quote is not None:
                return quote

            for product_url in self._fallback_product_urls(store=store, query=query_variant):
                try:
                    product_response = await client.get(product_url)
                    product_response.raise_for_status()
                except Exception:
                    continue
                quote = self._extract_store_quote(
                    store=store,
                    html=product_response.text,
                    query=query_variant,
                    url=str(product_response.url),
                )
                if quote is not None:
                    return quote
        return None

    def _extract_store_quote(
        self,
        *,
        store: str,
        html: str,
        query: str,
        url: str,
    ) -> dict[str, str | None] | None:
        if store == "continente":
            quote = self._extract_continente_quote(html=html, query=query)
            if quote is not None:
                return quote
        if store == "lidl":
            quote = self._extract_lidl_quote(html=html, query=query, url=url)
            if quote is not None:
                return quote

        text = self._compact(html)
        price = self._extract_price(text)
        if price is None or self._invalid_price(price):
            return None
        old_price = self._extract_old_price(text, price)
        title = self._extract_title(text) or query
        if not self._usable_title(title=title, query=query):
            return None
        return {
            "product_name": title[:500],
            "price": price,
            "old_price": old_price,
            "url": url,
        }

    def _stores_for_item(self, item: PriceScanItem) -> list[str]:
        store_name = item.store_name
        if store_name:
            lowered = store_name.lower()
            for store in self.MAIN_STORES:
                if store in lowered:
                    return [store]
        return [store for store in self.MAIN_STORES if store != "minimix"]

    def _query_variants(self, query: str) -> list[str]:
        normalized = self._normalize_item_name(query)
        variants = [query.strip()]
        for source, aliases in self.QUERY_ALIASES.items():
            if source in normalized:
                variants.extend(aliases)
        return list(dict.fromkeys(variant for variant in variants if variant))

    def _fallback_product_urls(self, *, store: str, query: str) -> tuple[str, ...]:
        normalized = self._normalize_item_name(query)
        urls: list[str] = []
        for key, product_urls in self.FALLBACK_PRODUCT_URLS.get(store, {}).items():
            if key in normalized:
                urls.extend(product_urls)
        return tuple(dict.fromkeys(urls))

    def _extract_continente_quote(
        self,
        *,
        html: str,
        query: str,
    ) -> dict[str, str | None] | None:
        product_blocks = re.findall(
            r"(<div class=\"product-tile\b.*?)(?=<div class=\"product-tile\b|$)",
            html,
            flags=re.DOTALL,
        )
        for block in product_blocks:
            impression_match = re.search(r"data-product-tile-impression='([^']+)'", block)
            href_match = re.search(r'href="([^"]*/produto/[^"]+)"', block)
            if not impression_match:
                continue
            impression = unescape(impression_match.group(1))
            name_match = re.search(r'"name":"([^"]+)"', impression)
            price_match = re.search(r'"price":([0-9]+(?:\.[0-9]+)?)', impression)
            if not name_match or not price_match:
                continue
            if self._invalid_price(price_match.group(1)):
                continue
            title = name_match.group(1)
            if not self._usable_title(title=title, query=query):
                continue
            return {
                "product_name": title[:500],
                "price": self._format_price(price_match.group(1)),
                "old_price": self._extract_continente_old_price(block, price_match.group(1)),
                "url": href_match.group(1) if href_match else None,
            }

        title = self._extract_jsonld_value(html, "name")
        price = self._extract_jsonld_value(html, "price")
        if title and price and not self._invalid_price(price) and self._usable_title(title=title, query=query):
            canonical = self._extract_canonical_url(html)
            return {
                "product_name": title[:500],
                "price": self._format_price(price),
                "old_price": self._extract_old_price(self._compact(html), self._format_price(price)),
                "url": canonical,
            }
        return None

    def _extract_lidl_quote(
        self,
        *,
        html: str,
        query: str,
        url: str,
    ) -> dict[str, str | None] | None:
        title = self._extract_meta_content(html, "title")
        description = self._extract_meta_content(html, "description") or ""
        og_description = self._extract_meta_property(html, "og:description") or ""
        text = "\n".join([description, og_description, self._compact(html)])

        if not title or not self._usable_title(title=title, query=query):
            return None

        price = self._extract_price(text)
        if price is None or self._invalid_price(price):
            return None
        old_price = self._extract_lidl_old_price(html=html, current_price=price)
        return {
            "product_name": title[:500],
            "price": price,
            "old_price": old_price,
            "url": self._extract_canonical_url(html) or url,
        }

    @staticmethod
    def _extract_continente_old_price(block: str, current_price: str) -> str | None:
        old_match = re.search(
            r"(?:old|strike|list|previous|price-standard)[^<]{0,200}?(\d+,\d{2})",
            block,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not old_match:
            return None
        old_price = old_match.group(1).replace(",", ".")
        return old_price if old_price != PriceService._format_price(current_price) else None

    @staticmethod
    def _extract_lidl_old_price(*, html: str, current_price: str) -> str | None:
        html = unescape(html)
        patterns = (
            r'"oldPrice"[^0-9]{0,80}(\d+[,.]\d{2})',
            r'price__stroke[^<]{0,300}<s[^>]*>\s*(\d+[,.]\d{2})\s*</s>',
            r'~~\s*(\d+[,.]\d{2})\s*~~',
        )
        current = PriceService._format_price(current_price)
        for pattern in patterns:
            match = re.search(pattern, html, flags=re.IGNORECASE | re.DOTALL)
            if match:
                old_price = PriceService._format_price(match.group(1))
                if old_price != current:
                    return old_price
        return None

    @staticmethod
    def _extract_meta_content(html: str, name: str) -> str | None:
        match = re.search(
            rf'<meta\s+name="{re.escape(name)}"\s+content="([^"]+)"',
            html,
            flags=re.IGNORECASE,
        )
        return unescape(match.group(1)).strip() if match else None

    @staticmethod
    def _extract_meta_property(html: str, property_name: str) -> str | None:
        match = re.search(
            rf'<meta\s+property="{re.escape(property_name)}"\s+content="([^"]+)"',
            html,
            flags=re.IGNORECASE,
        )
        return unescape(match.group(1)).strip() if match else None

    @staticmethod
    def _extract_canonical_url(html: str) -> str | None:
        match = re.search(r'<link\s+rel="canonical"\s+href="([^"]+)"', html, flags=re.IGNORECASE)
        return unescape(match.group(1)).strip() if match else None

    @staticmethod
    def _extract_jsonld_value(html: str, key: str) -> str | None:
        match = re.search(rf'"{re.escape(key)}"\s*:\s*"([^"]+)"', html)
        if match:
            return unescape(match.group(1)).strip()
        number_match = re.search(rf'"{re.escape(key)}"\s*:\s*([0-9]+(?:\.[0-9]+)?)', html)
        return number_match.group(1) if number_match else None

    @staticmethod
    def _compact(html: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", unescape(html)))

    @staticmethod
    def _extract_price(text: str) -> str | None:
        patterns = (
            r"(\d+,\d{2})\s*€",
            r"€\s*(\d+,\d{2})",
            r"\b(\d+\.\d{2})\b",
            r"\b(\d+)\s+(\d{2})\b",
        )
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                if len(match.groups()) == 2:
                    return f"{match.group(1)}.{match.group(2)}"
                return match.group(1).replace(",", ".")
        return None

    @staticmethod
    def _extract_old_price(text: str, current_price: str) -> str | None:
        prices = []
        for match in re.finditer(r"(\d+,\d{2})\s*€|€\s*(\d+,\d{2})", text):
            raw = match.group(1) or match.group(2)
            if raw:
                prices.append(raw.replace(",", "."))
        for price in prices:
            if price != current_price:
                return price
        return None

    @staticmethod
    def _extract_title(text: str) -> str | None:
        match = re.search(r"([A-ZÁÉÍÓÚÂÊÔÃÕÇ][A-Za-zÀ-ÿ0-9 ,.'®-]{8,90})", text)
        if not match:
            return None
        return match.group(1).strip()

    @staticmethod
    def _usable_title(*, title: str, query: str) -> bool:
        lowered_title = title.lower()
        return not (
            "resultado" in lowered_title
            or "pesquisou por" in lowered_title
            or "elementsbytagname" in lowered_title
            or lowered_title in {"document", "window", "undefined"}
            or not PriceService._title_matches_query(title, query)
        )

    @staticmethod
    def _title_matches_query(title: str, query: str) -> bool:
        title_tokens = {token for token in re.split(r"\W+", title.lower()) if len(token) >= 4}
        query_tokens = {token for token in re.split(r"\W+", query.lower()) if len(token) >= 4}
        if not query_tokens:
            return True
        return bool(title_tokens & query_tokens)

    @staticmethod
    def _format_price(value: str) -> str:
        return value.replace(",", ".").strip()

    @staticmethod
    def _invalid_price(value: str) -> bool:
        try:
            return float(value.replace(",", ".").strip()) <= 0
        except ValueError:
            return True

    @staticmethod
    def _normalize_item_name(value: str) -> str:
        return re.sub(r"\W+", " ", value.lower()).strip()
