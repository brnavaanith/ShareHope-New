"""Shared donation category taxonomy (labels only — no donation records).

Used by home, explore, requirements and dashboard filters so the eight
categories stay consistent everywhere. Real donations/requirements come
from MySQL in a later phase; this file invents no listings.
"""

CATEGORIES = [
    {"slug": "books", "name": "Books & Education", "glyph": "§", "blurb": "Textbooks, guides, storybooks — grade-wise and condition-graded."},
    {"slug": "food", "name": "Food & Groceries", "glyph": "◆", "blurb": "Staples and sealed packs with expiry dates; fresh pickups prioritised."},
    {"slug": "clothing", "name": "Clothing", "glyph": "◍", "blurb": "Washed, size-sorted seasonal wear, uniforms and infant sets."},
    {"slug": "electronics", "name": "Electronics", "glyph": "▣", "blurb": "Working phones, laptops and appliances with tested condition notes."},
    {"slug": "toys", "name": "Toys & Games", "glyph": "●", "blurb": "Complete, clean play and learning kits for children of all ages."},
    {"slug": "home", "name": "Home Essentials", "glyph": "⬣", "blurb": "Bedding, utensils and daily-use items for new households."},
    {"slug": "furniture", "name": "Furniture", "glyph": "▤", "blurb": "Sturdy tables, chairs, beds and storage — pickup-planned."},
    {"slug": "shoes", "name": "Shoes & Accessories", "glyph": "⬔", "blurb": "Paired, wearable footwear and everyday accessories by size."},
]

# Backwards-compatible short names used by older mock rows/filters.
LEGACY_TO_SLUG = {"Books": "books", "Food": "food", "Clothes": "clothing"}


def category_names():
    return [c["name"] for c in CATEGORIES]
