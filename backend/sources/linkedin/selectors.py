"""Every LinkedIn selector, central, with fallback chains.

LinkedIn's DOM shifts frequently; each logical element lists selectors
newest-first and parsing walks the chain until one matches. Parsing
failures degrade per-job (skip + log) — they never crash a run.
"""

from bs4 import BeautifulSoup, Tag

SELECTORS: dict[str, list[str]] = {
    # --- search results page ---
    "search_card": [
        "li[data-occludable-job-id]",
        "div[data-job-id]",
        "li.jobs-search-results__list-item",
    ],
    "card_title": [
        "a.job-card-list__title--link span[aria-hidden='true']",
        "a.job-card-container__link span[aria-hidden='true']",
        ".job-card-list__title",
        ".artdeco-entity-lockup__title",
    ],
    "card_link": [
        "a.job-card-list__title--link",
        "a.job-card-container__link",
        "a[href*='/jobs/view/']",
    ],
    "card_company": [
        ".artdeco-entity-lockup__subtitle",
        ".job-card-container__primary-description",
        ".job-card-container__company-name",
    ],
    "card_location": [
        ".job-card-container__metadata-wrapper li",
        "ul.job-card-container__metadata-list li",
        ".job-card-container__metadata-item",
    ],
    "card_posted": [
        "time[datetime]",
        ".job-card-container__listed-time",
    ],
    "card_footer_items": [
        ".job-card-container__footer-item",
        ".job-card-list__footer-wrapper li",
    ],
    # --- job detail page ---
    "detail_title": [
        ".job-details-jobs-unified-top-card__job-title",
        ".jobs-unified-top-card__job-title",
        "h1.t-24",
    ],
    "detail_description": [
        "#job-details",
        ".jobs-description__content",
        ".jobs-box__html-content",
    ],
    # --- session state probes ---
    "logged_in_marker": [
        "header.global-nav",
        "#global-nav",
        "div.global-nav__me",
    ],
    "login_form": [
        "form.login__form",
        "input#username",
        "form[action*='login']",
    ],
}

# URL substrings that mean LinkedIn is challenging or blocking the
# session. On any of these we stop cleanly — never any bypass attempt.
CHECKPOINT_URL_MARKERS = (
    "/checkpoint/",
    "/authwall",
    "/uas/login",
    "captcha",
    "/security/challenge",
)


def is_checkpoint_url(url: str) -> bool:
    lowered = url.lower()
    return any(marker in lowered for marker in CHECKPOINT_URL_MARKERS)


def parse_html(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def select_first(root: Tag, key: str) -> Tag | None:
    for selector in SELECTORS[key]:
        hit = root.select_one(selector)
        if hit is not None:
            return hit
    return None


def select_all(root: Tag, key: str) -> list[Tag]:
    for selector in SELECTORS[key]:
        hits = root.select(selector)
        if hits:
            return hits
    return []


def text_of(node: Tag | None) -> str | None:
    if node is None:
        return None
    text = " ".join(node.get_text(separator=" ").split())
    return text or None
