#!/usr/bin/env python3
"""Unit test for list_posts dedup — no network. Run: .venv/bin/python test_list_posts_dedup.py

Reproduces the bug where Skool prepends pinned posts to the top of `postTrees`
while ALSO leaving each pinned post in its natural chronological slot, so the raw
array carries duplicate ids. list_posts must dedup by id before slicing to limit.
"""
import skool_client as sc

# Mirrors the real feed: two pinned posts at top (pinned: 1), then the same two
# posts appear again later in their chronological positions (no pin flag).
FAKE_PP = {
    "postTrees": [
        {"post": {"id": "PIN1", "name": "pin-1", "metadata": {"title": "Pinned One", "pinned": 1}}},
        {"post": {"id": "PIN2", "name": "pin-2", "metadata": {"title": "Pinned Two", "pinned": 1}}},
        {"post": {"id": "PIN1", "name": "pin-1", "metadata": {"title": "Pinned One"}}},  # chrono dup
        {"post": {"id": "A", "name": "a", "metadata": {"title": "Post A"}}},
        {"post": {"id": "B", "name": "b", "metadata": {"title": "Post B"}}},
        {"post": {"id": "PIN2", "name": "pin-2", "metadata": {"title": "Pinned Two"}}},  # chrono dup
        {"post": {"id": "C", "name": "c", "metadata": {"title": "Post C"}}},
    ],
    "total": 159,
}


def run():
    sc._page_props = lambda path: FAKE_PP  # stub the network

    # limit=3 must yield 3 DISTINCT posts, not [PIN1, PIN2, PIN1].
    res = sc.list_posts(limit=3)
    posts = res["posts"]
    ids = [p["id"] for p in posts]

    assert len(ids) == len(set(ids)), f"duplicate ids returned: {ids}"
    assert len(posts) == 3, f"expected 3 posts, got {len(posts)}"
    assert ids == ["PIN1", "PIN2", "A"], f"pinned-first order broken: {ids}"
    assert res["count"] == 3, f"count mismatch: {res['count']}"

    # A larger limit must surface every unique post exactly once (7 entries -> 5 unique).
    res2 = sc.list_posts(limit=20)
    ids2 = [p["id"] for p in res2["posts"]]
    assert ids2 == ["PIN1", "PIN2", "A", "B", "C"], f"unexpected full set: {ids2}"
    assert len(ids2) == len(set(ids2)), f"duplicates in full set: {ids2}"

    print("PASS — list_posts dedups pinned posts and preserves pinned-first order")


if __name__ == "__main__":
    run()
