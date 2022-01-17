from threading import Lock
from time import time
from urllib.parse import quote_plus

from cachetools import cached
from requests import Session
from telegram import InlineKeyboardButton
from yarl import URL

from reverse_image_search_bot.settings import SAUCENAO_API
from reverse_image_search_bot.utils import tagify, url_button

from .generic import GenericRISEngine
from .types import InternalProviderData, MetaData, ProviderData


class SauceNaoEngine(GenericRISEngine):
    name = "SauceNAO"
    description = (
        "SauceNAO is a reverse image search website which is widely used to find the source of animes, mangas and"
        " related fanart."
    )
    provider_url = URL("https://saucenao.com/")
    types = ["Anime/Manga related Artworks", "Anime", "Manga"]
    recommendation = ["Anime", "Manga", "Anime/Manga related Artworks"]

    url = "https://saucenao.com/search.php?url={query_url}"
    limit_reached = None

    ResponseData = dict[str, str | int | list[str]]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.session = Session()
        self.lock = Lock()

    def _21_provider(self, data: ResponseData) -> InternalProviderData:
        """Anime"""
        buttons: list[InlineKeyboardButton] = []

        meta: MetaData = {}
        result = {}
        if "anilist_id" in data:
            result, meta = self._anilist_provider(data["anilist_id"], data.get("part"))  # type: ignore
            if result:
                buttons = meta.get("buttons", [])
                for item in data.get("ext_urls", []):  # type: ignore
                    if "anilist.co" not in item:
                        buttons.append(url_button(item))

        if not result:
            for item in data.get("ext_urls", []):  # type: ignore
                buttons.append(url_button(item))

            result.update(
                {
                    "Source": data["source"],
                    "Episode": data["part"],
                }
            )

        result.update(
            {
                "Year": data["year"],
                "Est. Time": data["est_time"],
            }
        )

        meta["buttons"] = buttons
        return result, meta

    def _5_provider(self, data: ResponseData) -> InternalProviderData:
        """Pixiv"""
        return (
            {"Title": data["title"], "Creator": data["member_name"]},
            {
                "buttons": [
                    url_button(f"https://www.pixiv.net/en/artworks/{data['pixiv_id']}", text="Source"),
                    url_button(f"https://www.pixiv.net/en/users/{data['member_id']}", text="Artist"),
                ]
            },
        )

    def _booru_provider(self, data: ResponseData, provider_name: str) -> InternalProviderData:
        """Generic Booru SauceNAO Provider"""
        buttons: list[InlineKeyboardButton] = []
        result = {}
        meta: MetaData = {}

        if provider_name + "_id" in data:
            result, meta = getattr(self, f"_{provider_name}_provider")(data["danbooru_id"])  # type: ignore
            if meta:
                buttons = meta.get("buttons", [])

        if not result:
            if source := data.get("source"):
                buttons.append(url_button(source, text="Source"))  # type: ignore

            for item in data.get("ext_urls", []):  # type: ignore
                buttons.append(url_button(item))

            result.update(
                {
                    "Character": tagify(data.get("characters")),  # type: ignore
                    "Material": data.get("material"),
                    "By": tagify(data.get("creator")),  # type: ignore
                }
            )

        meta["buttons"] = buttons
        return result, meta

    def _9_provider(self, data: ResponseData) -> InternalProviderData:
        """Danbooru"""
        return self._booru_provider(data, "danbooru")

    def _12_provider(self, data: ResponseData) -> InternalProviderData:
        """Yandere"""
        return self._booru_provider(data, "yandere")

    def _25_provider(self, data: ResponseData) -> InternalProviderData:
        """Gelbooru"""
        return self._booru_provider(data, "gelbooru")

    def _default_provider(self, data: ResponseData) -> InternalProviderData:
        """Generic"""
        buttons: list[InlineKeyboardButton] = []
        for item in data.pop("ext_urls", []):  # type: ignore
            buttons.append(url_button(item))

        result = {}
        meta = {"buttons": buttons}

        skip = []
        for key, value in list(data.items()):
            if key in skip:
                continue
            match key:
                case k if k.endswith(("_id", "_aid")):
                    continue
                case k if k.endswith("_url"):  # author_name + author_url fields
                    clean_key = key.replace("_url", "")
                    name = ""
                    if (alt_key := clean_key + "_name") in data:
                        name = clean_key.title()
                        skip.append(alt_key)
                        result.pop(alt_key.replace("_", " ").title(), None)
                    buttons.append(url_button(str(value), text=name))
                case "twitter_user_handle":
                    result["Poster"] = value.title()  # type: ignore
                    buttons.append(url_button(f"https://twitter.com/{value}", text=value.title()))  # type: ignore
                case _:
                    result[key.replace("_", " ").title()] = value

        return result, meta  # type: ignore

    @cached(GenericRISEngine._best_match_cache)
    def best_match(self, url: str | URL) -> ProviderData:
        self.logger.debug("Started looking for %s", url)
        meta: MetaData = {
            "provider": self.name,
            "provider_url": self.provider_url,
        }
        limit_reached_result = (
            "Daily limit reached. You can search SauceNAO via it's button above or <b>More</b> below."
        )

        if self.limit_reached and time() - self.limit_reached < 3600:
            meta["errors"] = [limit_reached_result]
            self.logger.debug("Done with search: found nothing")
            return {}, meta

        api_link = "https://saucenao.com/search.php?db=999&output_type=2&testmode=1&numres=8&url={}{}".format(
            quote_plus(str(url)), f"&api_key={SAUCENAO_API}" if SAUCENAO_API else ""
        )
        with self.lock:
            response = self.session.get(api_link)

        if response.status_code == 429:
            self.limit_reached = time()
            meta["errors"] = [limit_reached_result]
            self.logger.debug("Done with search: found nothing")
            return {}, meta

        if response.status_code != 200:
            self.logger.debug("Done with search: found nothing")
            return {}, {}

        results = filter(lambda d: float(d["header"]["similarity"]) >= 60, response.json().get("results", []))

        priority = 21, 5, 9, 12, 25  # Anime, Pixiv, Danbooru, Yandere, Gelbooru
        data = next(
            iter(
                sorted(
                    results,
                    key=lambda r: (
                        priority.index(r["header"]["index_id"]) if r["header"]["index_id"] in priority else 99,
                        float(r["header"]["similarity"]) * -1,
                    ),
                )
            ),
            None,
        )

        if not data:
            self.logger.debug("Done with search: found nothing")
            return {}, {}

        data_provider = getattr(self, f"_{data['header']['index_id']}_provider", self._default_provider)
        result, new_meta = data_provider(data["data"])
        meta.update(new_meta)

        meta.update(
            {
                "thumbnail": URL(meta.get("thumbnail", data["header"]["thumbnail"])),
                "similarity": float(data["header"]["similarity"]),
            }
        )

        self.logger.debug("Done with search: found something")
        return self._clean_privider_data(result), meta
