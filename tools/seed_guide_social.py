"""One-off seeder: push the editorial guides into the blog social queue.

The guides live in the website repo (`tools/category-map.json`), not here, so
this script reads each guide's `og:title` / `og:image` straight off the live page
instead of duplicating a catalog that would go stale. Everything else — the
06:00-22:00 random draw, the 45-min gap from the fixed product posts, the 3/day
cap — is the existing `queue_blog_social_post` behaviour, untouched.

Usage (needs DATABASE_URL unless --dry-run):
    python -m tools.seed_guide_social maakav-mishlochim-amazon-israel
    python -m tools.seed_guide_social --all-except maakav-mishlochim-amazon-israel
    python -m tools.seed_guide_social --all --dry-run
"""
import argparse
import asyncio
import html
import re
import sys

import httpx

BASE = "https://www.amzfreeil.com/blog"

# Order matches guides.catalog in the website repo's tools/category-map.json.
GUIDE_SLUGS = [
    "mishloach-hinam-amazon-israel",
    "mekhs-umaam-amazon-israel",
    "hamutzarim-hakhi-kedaim-laknot-bamazon-israel",
    "hacharot-amazon-israel",
    "eich-ladaat-mishloach-hinam-amazon-israel",
    "amazon-prime-mishloach-israel",
    "madrikh-kahniot-amazon-israel-2026",
    "black-friday-prime-day-israel",
    "bikorot-mezuyafot-amazon",
    "mutzarim-asurim-yevu-israel",
    "10-tipim-lehisakhon-bamazon-israel",
    "amazon-vs-ebay-israel",
    "amazon-vs-aliexpress-israel",
    "maakav-mishlochim-amazon-israel",
]


def _meta(page: str, prop: str) -> str:
    """Pull an og: meta value, tolerating either attribute order."""
    for pattern in (
        rf'<meta[^>]+property=["\']{prop}["\'][^>]+content=["\']([^"\']+)["\']',
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']{prop}["\']',
    ):
        m = re.search(pattern, page, re.IGNORECASE)
        if m:
            return html.unescape(m.group(1)).strip()
    return ""


async def fetch_guide(client: httpx.AsyncClient, slug: str) -> tuple[str, str]:
    """Return (title, image_url) for a guide, raising when the page or image is broken."""
    resp = await client.get(f"{BASE}/{slug}.html")
    resp.raise_for_status()
    title = _meta(resp.text, "og:title")
    image = _meta(resp.text, "og:image")
    if not title:
        raise RuntimeError(f"{slug}: no og:title on the page")
    if not image:
        raise RuntimeError(f"{slug}: no og:image on the page")
    head = await client.head(image, follow_redirects=True)
    if head.status_code != 200:
        raise RuntimeError(f"{slug}: og:image returned {head.status_code} — {image}")
    return title, image


async def main(slugs: list[str], dry_run: bool) -> int:
    failures = 0
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        for slug in slugs:
            try:
                title, image = await fetch_guide(client, slug)
            except Exception as e:
                print(f"[FAIL] {e}")
                failures += 1
                continue

            if dry_run:
                print(f"[dry-run] {slug}\n          title: {title}\n          image: {image}")
                continue

            from backend.scheduler import queue_blog_social_post

            scheduled_at, warnings = await queue_blog_social_post(
                asin=None, slug=slug, title=title, image_url=image, kind="guide",
            )
            note = f"  ⚠️ {'; '.join(warnings)}" if warnings else ""
            print(f"[queued] {scheduled_at.isoformat()}  {slug}{note}")
    return failures


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Queue editorial guides for Telegram/Facebook")
    ap.add_argument("slugs", nargs="*", help="guide slugs to queue")
    ap.add_argument("--all", action="store_true", help="queue every guide")
    ap.add_argument("--all-except", metavar="SLUG", action="append", default=[],
                    help="queue every guide except this one (repeatable)")
    ap.add_argument("--dry-run", action="store_true", help="fetch and print, write nothing")
    args = ap.parse_args()

    if args.all:
        targets = list(GUIDE_SLUGS)
    elif args.all_except:
        targets = [s for s in GUIDE_SLUGS if s not in args.all_except]
    else:
        targets = args.slugs

    if not targets:
        ap.error("nothing to do — pass slugs, --all, or --all-except")

    unknown = [s for s in targets if s not in GUIDE_SLUGS]
    if unknown:
        ap.error(f"unknown guide slug(s): {', '.join(unknown)}")

    sys.exit(1 if asyncio.run(main(targets, args.dry_run)) else 0)
