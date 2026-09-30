#!/usr/bin/env python3
"""Seed a page-level snapshot from real Search Console data.

Only needed to demonstrate the alert detail before the first live collector
run. Normal runs overwrite this file every time.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from monitor import store  # noqa: E402

# Current window: 2026-09-12 to 2026-09-18 (7 days)
CURRENT = """
/ 8687 25632
/brands/kayali 438 6303
/brands/medicube 402 9481
/fragrance 287 12034
/brands/sol-de-janeiro 258 10768
/makeup 222 14838
/brands 175 13168
/offers 152 11325
/our-stores 125 13099
/brands/huda-beauty 115 3290
/brands/k%C3%A9rastase 114 2163
/brands/fenty-beauty 113 2624
/brands/laneige 113 1580
/brands/calvin-klein 93 6970
/fragrance/mini 88 1170
/brands/salt-stone 85 467
/brands/the-ordinary 82 4346
/skincare 82 10969
/brands/yves-saint-laurent 79 5486
/brands/tom-ford 76 3603
/brands/guerlain 73 2867
/our-stores/sandton-city-1 72 1824
/brands/chanel 70 3606
/arc-gift-cards 68 209
/fragrance/men 67 10430
/products/brand/kayali/yum-marshmallow-edp 64 5423
/brands/anua 61 2147
/brands/shark-beauty 60 1023
/brands/maison-margiela-fragrances 57 895
/our-stores/menlyn 57 1740
/products/brand/kayali/vanilla-28-edp 57 1622
/brands/valentino 56 3568
/brands/skin1004 55 1472
/our-stores/gateway 55 1200
/brands/beauty-of-joseon 54 874
/k-beauty-at-arc 53 4850
/brands/it-cosmetics 51 506
/fragrance/women 50 14352
"""

# Baseline window: 2026-08-15 to 2026-09-11 (28 days)
BASELINE = """
/ 31799 106454
/brands/kayali 2148 24213
/brands/medicube 1775 32994
/fragrance 1227 50515
/brands/sol-de-janeiro 1171 30890
/offers 687 46172
/makeup 681 58216
/our-stores 625 53525
/brands 588 52666
/brands/k%C3%A9rastase 555 10809
/brands/laneige 485 6595
/brands/huda-beauty 484 9234
/products/brand/kayali/vanilla-28-edp 465 7470
/brands/guerlain 458 3669
/brands/the-ordinary 399 22478
/brands/anua 363 8528
/products/brand/kayali/yum-marshmallow-edp 361 4423
/arc-gift-cards 359 956
/brands/fenty-beauty 348 9547
/fragrance/mini 340 4665
/brands/beauty-of-joseon 330 4567
/k-beauty-at-arc 327 18374
/brands/tom-ford 300 17254
/brands/chanel 290 12893
/skincare 287 45995
/brands/calvin-klein 281 14583
/brands/yves-saint-laurent 278 16444
/our-stores/sandton-city-1 272 7551
/fragrance/men 267 34778
/brands/skin1004 262 7986
/brands/maison-margiela-fragrances 252 3878
/our-stores/menlyn 251 7297
/brands/salt-stone 230 1688
/our-stores/gateway 207 4643
/brands/it-cosmetics 206 2378
/brands/valentino 193 11212
/brands/shark-beauty 210 3117
"""

BASE_URL = "https://www.arcstore.co.za"


def parse(block: str) -> dict:
    out = {}
    for line in block.strip().splitlines():
        path, clicks, impressions = line.rsplit(" ", 2)
        url = BASE_URL + ("" if path == "/" else path) or BASE_URL + "/"
        if path == "/":
            url = BASE_URL + "/"
        out[url] = {"clicks": int(clicks), "impressions": int(impressions)}
    return out


def main(slug: str = "arcstore") -> None:
    snapshot = store.load_snapshot(slug)
    snapshot.update(
        {
            "domain": "www.arcstore.co.za",
            "as_of": "2026-09-18",
            "pages_current": parse(CURRENT),
            "pages_baseline": parse(BASELINE),
            "baseline_scale": 4,
            "source_status": {"gsc": {"ok": True, "note": "seeded"}},
        }
    )
    store.save_snapshot(slug, snapshot)
    print(
        f"Seeded {len(snapshot['pages_current'])} current and "
        f"{len(snapshot['pages_baseline'])} baseline pages for {slug}"
    )


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "arcstore")
