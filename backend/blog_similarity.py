"""Flag blog candidates that are a variant of a product already covered.

Amazon lists the same product line under many ASINs (pack sizes, colours, CPU
options). Writing a post per ASIN produced near-identical template pages — 11
Calvin Klein sock posts, 2 BOSGAME E5 posts — which Google files under
"Crawled - currently not indexed" and which compete for the same query.

This is a warning signal for the admin, not a block, so it is tuned to catch
obvious variants and accept the occasional false positive.

Rule: same "brand key" (first two title tokens — usually brand + line/model,
e.g. "bosgame e5", "calvin klein") AND at least two shared content tokens among
the first 10 tokens after the brand key.
"""
import re

_FILLER = {
    "a", "an", "and", "the", "of", "for", "with", "in", "to", "by", "on", "or",
    "pack", "pk", "set", "new", "count", "ct", "pcs", "piece", "pieces",
    "inch", "oz", "lb", "lbs", "ft", "x",
}
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(name: str) -> list[str]:
    return _TOKEN_RE.findall((name or "").lower().replace("'", ""))


def similar_key(name: str) -> tuple[str, frozenset[str]] | None:
    toks = _tokens(name)
    if len(toks) < 3:
        return None
    brand = " ".join(toks[:2])
    content = frozenset(
        t for t in toks[2:12]
        if t not in _FILLER and not t.isdigit() and len(t) > 1
    )
    return brand, content


def is_similar(a: tuple[str, frozenset[str]] | None, b: tuple[str, frozenset[str]] | None) -> int:
    """Return the number of shared content tokens if a and b look like variants, else 0."""
    if not a or not b or a[0] != b[0]:
        return 0
    shared = len(a[1] & b[1])
    return shared if shared >= 2 else 0


def find_similar(name: str, existing: list[dict]) -> dict | None:
    """existing: dicts with at least a "name" key. Returns the closest match or None."""
    key = similar_key(name)
    if not key:
        return None
    best, best_score = None, 0
    for item in existing:
        score = is_similar(key, item.get("_key") or similar_key(item.get("name", "")))
        if score > best_score:
            best, best_score = item, score
    return best
