import re
from dataclasses import dataclass
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
        url = url_template.format(query=quote_plus(query))
        try:
            response = await client.get(url)
            response.raise_for_status()
        except Exception:
            return None
        text = self._compact(response.text)
        price = self._extract_price(text)
        if price is None:
            return None
        old_price = self._extract_old_price(text, price)
        title = self._extract_title(text) or query
        lowered_title = title.lower()
        if (
            "resultado" in lowered_title
            or "pesquisou por" in lowered_title
            or not self._title_matches_query(title, query)
        ):
            return None
        return {
            "product_name": title[:500],
            "price": price,
            "old_price": old_price,
            "url": str(response.url),
        }

    def _stores_for_item(self, item: PriceScanItem) -> list[str]:
        store_name = item.store_name
        if store_name:
            lowered = store_name.lower()
            for store in self.MAIN_STORES:
                if store in lowered:
                    return [store]
        return [store for store in self.MAIN_STORES if store != "minimix"]

    @staticmethod
    def _compact(html: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))

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
    def _title_matches_query(title: str, query: str) -> bool:
        title_tokens = {token for token in re.split(r"\W+", title.lower()) if len(token) >= 4}
        query_tokens = {token for token in re.split(r"\W+", query.lower()) if len(token) >= 4}
        if not query_tokens:
            return True
        return bool(title_tokens & query_tokens)

    @staticmethod
    def _normalize_item_name(value: str) -> str:
        return re.sub(r"\W+", " ", value.lower()).strip()
