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
        ".jobs-description-content__text",
        "article.jobs-description__container",
        ".show-more-less-html__markup",
        "[class*='jobs-description']",
    ],
    # --- results-page state markers ---
    "results_list": [
        "ul.jobs-search-results__list",
        ".jobs-search-results-list",
        ".scaffold-layout__list",
    ],
    "no_results_banner": [
        ".jobs-search-no-results-banner",
        ".jobs-search-two-pane__no-results-banner",
        "section.jobs-search-no-results",
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
    "checkpoint_marker": [
        "#captcha-internal",
        ".challenge-dialog",
        "form#challenge-form",
        "main.checkpoint",
    ],
}

# URL substrings that mean LinkedIn is challenging or blocking the
# session. On any of these we stop cleanly — never any bypass attempt.
# (Plain login URLs are NOT here — an expired session classifies as
# logged_out, which asks the user to sign in rather than bannering a
# checkpoint.)
CHECKPOINT_URL_MARKERS = (
    "/checkpoint/",
    "/authwall",
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


def classify_page(url: str, html: str) -> str:
    """(url, html) -> 'captcha_stop' | 'ready' | 'logged_out' | 'unknown'.

    Pure function over strings so tests exercise the exact production
    classification without a browser."""
    if is_checkpoint_url(url):
        return "captcha_stop"
    soup = parse_html(html)
    if select_first(soup, "checkpoint_marker") is not None:
        return "captcha_stop"
    if select_first(soup, "login_form") is not None or "/login" in url.lower():
        return "logged_out"
    # Still ON the feed = signed in: LinkedIn redirects unauthenticated
    # visitors off /feed/. URL-based and therefore robust to nav DOM
    # changes (observed live: global-nav selectors missed a real feed).
    if "linkedin.com/feed" in url.lower():
        return "ready"
    if select_first(soup, "logged_in_marker") is not None:
        return "ready"
    return "unknown"


def classify_empty_results(html: str) -> str:
    """Why did a results page parse to zero cards?
    'no_results' — LinkedIn explicitly says there is nothing (genuine
    end); anything else is 'unknown' (expired session already handled
    upstream; broken selectors or a lazy-load failure land here) and
    must be treated as an error, never as successful completion."""
    soup = parse_html(html)
    if select_first(soup, "no_results_banner") is not None:
        return "no_results"
    return "unknown"
