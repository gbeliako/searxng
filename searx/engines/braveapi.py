# SPDX-License-Identifier: AGPL-3.0-or-later
"""Engine to search using the Brave (WEB) Search API.

.. _Brave Search API: https://api-dashboard.search.brave.com/api-reference/web/search/get

Configuration
=============

The engine has the following mandatory setting:

- :py:obj:`api_key`

Optional settings are:

- :py:obj:`results_per_page`
- :py:obj:`brave_category` (search, videos, images, news, goggles)

.. code:: yaml

  - name: braveapi
    engine: braveapi
    api_key: 'YOUR-API-KEY'  # required
    results_per_page: 20     # optional
    brave_category: search

The API supports paging and time filters.
"""

import typing as t
from urllib.parse import urlencode
from dateutil import parser as dateutil_parser
import datetime

from searx.exceptions import SearxEngineAPIException
from searx.result_types import EngineResults
from searx.utils import html_to_text

from searx import locales
from searx.enginelib.traits import EngineTraits

if t.TYPE_CHECKING:
    from searx.extended_types import SXNG_Response
    from searx.search.processors import OnlineParams

about = {
    "website": "https://api.search.brave.com/",
    "wikidata_id": None,
    "official_api_documentation": "https://api-dashboard.search.brave.com/api-reference/web/search/get",
    "use_official_api": True,
    "require_api_key": True,
    "results": "JSON",
}

api_key: str = ""
categories = ["general", "web"]
paging = True
safesearch = True
time_range_support = True

results_per_page: int = 20
brave_category: t.Literal["search", "videos", "images", "news", "goggles"] = "search"
Goggles: str = ""

base_url = "https://api.search.brave.com/res/v1/web/search"
time_range_map = {"day": "past_day", "week": "past_week", "month": "past_month", "year": "past_year"}

countries = ["AR", "AU", "AT", "BE", "BR", "CA", "CL", "DK", "FI", "FR",
             "DE", "GR", "HK", "IN", "ID", "IT", "JP", "KR", "MY", "MX",
             "NL", "NZ", "NO", "CN", "PL", "PT", "PH", "RU", "SA", "ZA",
             "ES", "SE", "CH", "TW", "TR", "GB", "US"]

result_filter_map = {
    "search": ["web", "news", "videos", "images", "infobox", "locations",
                "discussions", "faq"],
    "videos": ["videos", "web"],
    "images": ["images", "web"],
    "news": ["news", "web"],
}

# ----------------------------------------------------------------------------
# Core extraction helpers
# ----------------------------------------------------------------------------

def _get_nested(data: dict, path: str, default=None):
    if not path:
        return data
    parts = path.split('.')
    value = data
    for part in parts:
        if isinstance(value, dict):
            value = value.get(part)
        else:
            return default
    return value if value is not None else default


def _extract_field(result: dict, field_def: tuple) -> t.Any:
    path, transform, default = field_def
    raw = _get_nested(result, path, None)
    if raw is None:
        return default
    if transform is None:
        return raw if raw is not None else default
    try:
        return transform(raw)
    except Exception:
        return default

# ----------------------------------------------------------------------------
# Generic collection builder
# ----------------------------------------------------------------------------

def _build_collection(source: dict, config: list, item_type: str = 'pair') -> list:
    """
    Build a list of items from source using config.
    config: list of (path, label_or_title, formatter_or_none)
      - path: dot-separated path in source
      - label_or_title: string or callable(source) -> string
      - formatter_or_none: callable(raw) -> string, or None
    item_type: 'pair' -> returns [{'label': ..., 'value': ...}]
                'url' -> returns [{'url': ..., 'title': ...}] (if raw is a single URL)
                         if raw is a list, each element is a URL.
    Returns list with non-empty values, skipping duplicates for 'url'.
    """
    result = []
    seen = set()

    for path, label_or_title, formatter in config:
        raw = _get_nested(source, path, None)
        if raw is None:
            continue

        if item_type == 'pair':
            value = formatter(raw) if formatter else raw
            if value:
                label = label_or_title(source) if callable(label_or_title) else label_or_title
                result.append({'label': label, 'value': value})
        else:  # 'url'
            urls = raw if isinstance(raw, list) else [raw]
            for url in urls:
                if url and url not in seen:
                    seen.add(url)
                    title = label_or_title(source) if callable(label_or_title) else label_or_title
                    result.append({'url': url, 'title': title})

    return result


# ----------------------------------------------------------------------------
# Transform functions (pure)
# ----------------------------------------------------------------------------

def _parse_duration(duration_str: str) -> datetime.timedelta:
    if not duration_str:
        return datetime.timedelta(0)
    try:
        parts = duration_str.split(':')
        if len(parts) == 2:
            return datetime.timedelta(minutes=int(parts[0]), seconds=int(parts[1]))
        elif len(parts) == 3:
            return datetime.timedelta(hours=int(parts[0]), minutes=int(parts[1]), seconds=int(parts[2]))
    except:
        pass
    return datetime.timedelta(0)


def _parse_published_date(date_str: str):
    if not date_str:
        return None
    try:
        #return Datetime(datetime.datetime.strptime(date_str, "%b %d, %Y"))
        #return Datetime(datetime.datetime.fromisoformat(date_str.replace('Z', '+00:00')))
        return datetime.datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        #return Datetime(dateutil_parser.parse(date_str))
    except:
        return None


def _format_rating(rating_obj: dict) -> str:
    rv = rating_obj.get('ratingValue')
    if not rv:
        return ''
    rating_str = str(rv)
    br = rating_obj.get('bestRating')
    if br:
        rating_str += f"/{br}"
    rc = rating_obj.get('reviewCount')
    if rc:
        rating_str += f" ({rc} reviews)"
    return rating_str


def _format_distance(dist_obj: dict) -> str:
    val = dist_obj.get('value')
    units = dist_obj.get('units')
    if val and units:
        return f"{val} {units}"
    return ''


def _format_movie_names(items: list) -> str:
    names = [i['name'] for i in items if isinstance(i, dict) and i.get('name')]
    return ', '.join(names) if names else ''


def _paper_authors(paper_obj: dict) -> list:
    return [html_to_text(a['name']) for a in paper_obj.get('author', []) if isinstance(a, dict) and a.get('name')]


def _paper_tags(paper_obj: dict) -> list:
    tags = []
    price = paper_obj.get('price', {})
    p = price.get('price')
    if p:
        cur = price.get('priceCurrency', '')
        tags.append(f"{p} {cur}".strip())
    rating = paper_obj.get('rating', {})
    rv = rating.get('ratingValue')
    if rv:
        rating_str = str(rv)
        br = rating.get('bestRating')
        if br:
            rating_str += f"/{br}"
        rc = rating.get('reviewCount')
        if rc:
            rating_str += f" ({rc} reviews)"
        tags.append(rating_str)
    return tags


def _location_address(location_obj: dict) -> dict:
    postal = location_obj.get('postal_address', {})
    return {
        'name': '',
        'road': postal.get('displayAddress', ''),
        'house_number': '',
        'locality': '',
        'postcode': '',
        'country': '',
    }

# ----------------------------------------------------------------------------
# Declarative configurations for structured fields
# ----------------------------------------------------------------------------

LOCATION_DATA_CONFIG = [
    ('contact.telephone', 'Phone: ', None),
    ('price_range', 'Price: ', None),
    ('rating', 'Rating: ', _format_rating),
    ('serves_cuisine', 'Cuisine: ', lambda v: ', '.join(v)),
]


INFOBOX_ATTR_CONFIG = [
    ('category', 'Category', None),
    ('page_age', 'Date', None),
    ('language', 'Language', None),
    ('subtype', 'Type', None),
    ('distance', 'Distance', _format_distance),
    ('movie.release', 'Release', None),
    ('movie.duration', 'Duration', None),
    ('movie.directors', 'Directors', _format_movie_names),
    ('movie.actors', 'Actors', _format_movie_names),
    ('movie.genre', 'Genres', lambda v: ', '.join(v)),
    ('profile.name', 'Profile Name', None),
    ('profile.long_name', 'Full Name', None),
    ('data.answer.author', 'Author', None),
    ('data.answer.upvoteCount', 'Upvotes', None),
]

PROFILES_CONFIG = [
    ('profiles', 'Profile',
     lambda p: [(it.get('name'), it.get('url'))
                for it in p if isinstance(it, dict) and it.get('url')]
               if isinstance(p, list) else []),
]


# For infobox, we only use found_in_urls for URLs (simplified)
#INFOBOX_URLS_CONFIG = [
#    ('found_in_urls', 'Source', None),
#]
#INFOBOX_URLS_CONFIG = [
#    ('found_in_urls', 'Source', None),
#    ('profiles', None, lambda p: [(item.get('name'), item.get('url'))
#                              for item in p if item.get('url')]
#                              if isinstance(p, list) else []),
#    ('profiles', None, lambda p: [(item.get('name'), item.get('url'))]
#                                 for item in p if item.get('url')
#                                   if isinstance(p, list) else []),
#]
# ----------------------------------------------------------------------------
# Field configurations per result type
# ----------------------------------------------------------------------------

TYPE_CONFIG = {
    'video': (
        'videos.html',
        {
            'url': ('url', None, ''),
            'title': ('title', html_to_text, ''),
            'content': ('description', html_to_text, ''),
            'publishedDate': ('page_age', _parse_published_date, None),
            'thumbnail': ('thumbnail.src', None, ''),
            'length': ('video.duration', _parse_duration, datetime.timedelta(0)),
            'views': ('video.views', None, 0),
            'metadata': (None, lambda r: ', '.join(r.get('video', {}).get('tags', [])), ''),
            'author': ('video.creator', None, ''),
            'priority': (None, lambda r: 'low', 'low'),
        }
    ),
    'image': (
        'images.html',
        {
            'url': ('url', None, ''),
            'title': ('title', html_to_text, ''),
            'publishedDate': ('page_age', _parse_published_date, None),
            'content': ('description', html_to_text, ''),
            'img_src': ('url', None, ''),
            'thumbnail': ('thumbnail.src', None, ''),
        }
    ),
    'location': (
        'map.html',
        {
            'url': ('url', None, ''),
            'title': ('title', html_to_text, ''),
            'publishedDate': ('page_age', _parse_published_date, None),
            'thumbnail': ('thumbnail.src', None, ''),
            'latitude': ('location.coordinates', lambda c: c[0] if c and len(c) > 0 else None, None),
            'longitude': ('location.coordinates', lambda c: c[1] if c and len(c) > 1 else None, None),
            'address': ('location', _location_address, {}),
            'data': ('location', lambda loc: _build_collection(loc, LOCATION_DATA_CONFIG, 'pair'), []),
            'priority': (None, lambda r: 'high', 'high'),
        }
    ),
    'infobox': (
        None,
        {
            'infobox': ('title', html_to_text, ''),
            'id': ('url', None, ''),
            'title': ('title', html_to_text, ''),
            'content': (None, lambda r: html_to_text(r.get('data', {}).get('answer', {}).get('text') or r.get('long_desc', '')), ''),
            'img_src': (None, lambda r: (
             _get_nested(r, 'thumbnail.src', '')
            or next((img.get('original') or img.get('src') for img in r.get('images') or [] if isinstance(img, dict)), '')
            ), ''),
            #'img_src': ('thumbnail.src', None, ''),
            'urls': (None, lambda r: _build_infobox_attributes(r, True, PROFILES_CONFIG), []),
            'attributes': (None, lambda r: _build_infobox_attributes(r), []),
            'category': ('category', None, ''),
        }
    ),
    'web': (
        None,
        {
            'url': ('url', None, ''),
            'title': ('title', html_to_text, ''),
            'content': ('description', html_to_text, ''),
            'publishedDate': ('page_age', _parse_published_date, None),
            'thumbnail': ('thumbnail.src', None, ''),
        }
    ),
    'discussion': (  # treated as web
        None,
        {
            'url': ('url', None, ''),
            'title': ('title', html_to_text, ''),
            'content': ('description', html_to_text, ''),
            'publishedDate': ('page_age', _parse_published_date, None),
            'thumbnail': ('thumbnail.src', None, ''),
        }
    ),
    'news': (  # treated as web
        None,
        {
            'url': ('url', None, ''),
            'title': ('title', html_to_text, ''),
            'content': ('description', html_to_text, ''),
            'publishedDate': ('page_age', _parse_published_date, None),
            'thumbnail': ('thumbnail.src', None, ''),
        }
    ),
    'faq': (
        'default.html',
        {
            'url': ('url', None, ''),
            'title': ('question', html_to_text, ''),
            'publishedDate': ('page_age', _parse_published_date, None),
            'content': ('answer', html_to_text, ''),
        }
    ),
    'software': (
        'packages.html',
        {
            'url': ('url', None, ''),
            'title': ('title', html_to_text, ''),
            'package_name': ('software.name', html_to_text, ''),
            'version': ('software.version', None, ''),
            'maintainer': ('software.author', html_to_text, ''),
            'publishedDate': ('software.datePublished', _parse_published_date, None),
            'content': ('description', html_to_text, ''),
            'homepage': ('software.homepage', None, ''),
            'source_code_url': ('software.codeRepository', None, ''),
            'thumbnail': ('thumbnail.src', None, ''),
            'tags': ('software.programmingLanguage', lambda v: [v] if v else [], []),
            'popularity': ('software', lambda r: f"Stars: {r.get('stars', '')} | Forks: {r.get('forks', '')}", ''),
        }
    ),
    'product': (
        'products.html',
        {
            'url': ('url', None, ''),
            'title': ('title', html_to_text, ''),
            'publishedDate': ('page_age', _parse_published_date, None),
            'content': ('description', html_to_text, ''),
            'thumbnail': ('thumbnail.src', None, ''),
            'price': ('product.offers', lambda r: f"{r[0].get('price', '')} {r[0].get('priceCurrency', '')}".strip() if r else '', ''),
            'shipping': (None, lambda r: '', ''),
            'source_country': (None, lambda r: '', ''),
            'category': ('product.category', None, ''),
        }
    ),
    'paper': (
        'paper.html',
        {
            'title': (None, lambda r: html_to_text(r.get('book', {}).get('title') or r.get('title')), ''),
            'url': ('url', None, ''),
            'content': ('description', html_to_text, ''),
            #'date_of_publication': ('page_age', _parse_published_date, None),
            'publishedDate': ('page_age', _parse_published_date, None),
            'authors': (None, lambda r: _paper_authors(r.get('book') or r.get('article', {})), []),
            'journal': (None, lambda r: html_to_text((r.get('book') or r.get('article', {})).get('publisher', {}).get('name', '')), ''),
            'pages': (None, lambda r: str((r.get('book') or r.get('article', {})).get('pages', '')), ''),
            'type': (None, lambda r: 'book' if r.get('subtype') == 'book' else 'article', 'article'),
            'thumbnail': ('thumbnail.src', None, ''),
            'tags': (None, lambda r: _paper_tags(r.get('book') or r.get('article', {})), []),
        }
    ),
}


# Helper for infobox attributes (merge API pairs with extra fields)
def _build_infobox_attributes(result: dict, url_mode: bool = False,
                              config: list = None) -> list:
    config = config if config is not None else INFOBOX_ATTR_CONFIG
    n_key = 'title' if url_mode else 'label'   # el único "check" que pides
    v_key = 'url'   if url_mode else 'value'
    out, existing = [], set()

    # parejas API 'attributes'
    if not url_mode:
        for pair in result.get('attributes', []):
            if isinstance(pair, list) and len(pair) >= 2:
                label = html_to_text(str(pair[0]))
                value = html_to_text(str(pair[1]))
                if label and value and label.lower() != 'generic':
                    out.append({n_key: label, v_key: value})
                    existing.add(label)

    # config: formatter puede devolver un str (un solo item)
    #         o una lista de (nombre, valor) -> varios items (p.ej. profiles)
    for path, label, formatter in config:
        raw = _get_nested(result, path, None)
        if raw is None:
            continue
        if formatter:
            try:
                transformed = formatter(raw)
            except Exception:
                transformed = None
            if isinstance(transformed, list):          # varios items
                for n, v in transformed:
                    if n and v and n not in existing:
                        out.append({n_key: n, v_key: v})
                        existing.add(n)
            elif transformed:                          # un solo item
                if label not in existing:
                    out.append({n_key: label, v_key: transformed})
                    existing.add(label)
        else:
            if label not in existing:
                out.append({n_key: label, v_key: str(raw)})
                existing.add(label)
    return out

# Map sections to types (discussions and news removed; they will default to 'web')
SECTION_TO_TYPE = {
    'videos': 'video',
    'images': 'image',
    'locations': 'location',
    'infobox': 'infobox',
    'faq': 'faq',
}

WEB_SUBTYPE_TO_TYPE = {
    'video': 'video',
    'location': 'location',
    'book': 'paper',
    'article': 'paper',
    'product': 'product',
    'software': 'software',
    'product_cluster': 'product',
}

def _get_result_type(section: str, result: dict) -> str:
    if section == 'web':
        subtype = result.get('subtype', '')
        return WEB_SUBTYPE_TO_TYPE.get(subtype, 'web')
    return SECTION_TO_TYPE.get(section, 'web')  # default 'web'


def _build_result(result: dict, template: str, fields_config: dict) -> dict:
    out = {}
    for key, field_def in fields_config.items():
        value = _extract_field(result, field_def)
        if value is not None:
            out[key] = value
    if template is not None:
        out['template'] = template
    return out

# ----------------------------------------------------------------------------
# Engine functions (unchanged)
# ----------------------------------------------------------------------------

def init(_):
    if not api_key:
        raise SearxEngineAPIException("No API key provided")


def add_language_support(search_args: dict, params: "OnlineParams") -> None:
    eng_lang = locales.get_engine_locale(params["searxng_locale"], traits.custom.get("ui_lang", {}), "en-us")
    if eng_lang:
        lang_parts = eng_lang.split("-")
        if len(lang_parts) >= 2:
            search_args["search_lang"] = lang_parts[0].lower()
            search_args["ui_lang"] = f"{lang_parts[0].lower()}-{lang_parts[1].upper()}"
            search_args["country"] = lang_parts[1].upper() if lang_parts[1].upper() in countries else "ALL"
        else:
            search_args["search_lang"] = eng_lang.lower()
    else:
        search_args["search_lang"] = "en"


def request(query: str, params: "OnlineParams") -> None:
    search_args: dict[str, str | int | None] = {
        "q": query,
        "count": results_per_page,
        "offset": (params["pageno"] - 1),
        "text_decorations": False,
    }
    if params["time_range"]:
        search_args["time_range"] = time_range_map.get(params["time_range"])
    if params["safesearch"]:
        search_args["safesearch"] = "strict"
    add_language_support(search_args, params)
    if brave_category == "goggles" and Goggles:
        search_args["goggles"] = Goggles
    params["url"] = f"{base_url}?{urlencode(search_args)}"
    params["headers"]["Accept"] = "application/json"
    params["headers"]["X-Subscription-Token"] = api_key


def response(resp: "SXNG_Response") -> EngineResults:
    res = EngineResults()
    data = resp.json()

    for section_name, section_data in data.items():
        if not isinstance(section_data, dict) or 'results' not in section_data:
            continue
        results_list = section_data['results']
        if not results_list:
            continue

        for result in results_list:
            result_type = _get_result_type(section_name, result)
            config = TYPE_CONFIG.get(result_type)
            if config is None:
                continue
            template, fields_config = config

            for src in ((result.get('product_cluster') or [result.get('product')])
                        if result_type == 'product' else [result]):
                built = _build_result(result, template, fields_config)
                if built:
                    res.add(built)

    return res
