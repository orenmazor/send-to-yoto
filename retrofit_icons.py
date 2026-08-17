"""One-off: set per-chapter icons on a Yoto playlist that already exists.

The service only picks icons at creation time. This backfills cards made before
that, or made by hand in the Yoto app.

Usage:

    task icons                        # list your cards
    task icons -- <cardId>            # dry run
    task icons -- <cardId> --apply    # write it

Without --apply it only prints what it would change. Runs in a one-off
container, so the service doesn't need to be up.

Reading a card back needs the user:content:view scope. It's in the default
scope set, but a token minted before that keeps the old scopes -- check
/healthz and run `task reauth` if it's missing.
"""

import sys

import app


def list_cards():
    for card in app.yoto("GET", "/content/mine").get("cards", []):
        print(f"  {card['cardId']}  {card.get('title')}")


def retrofit(card_id, apply=False):
    card = app.yoto("GET", f"/content/{card_id}")["card"]
    chapters = card.get("content", {}).get("chapters", [])
    if not chapters:
        sys.exit(f"{card_id} has no chapters")

    icons = app.public_icons()
    if not icons:
        sys.exit("could not fetch the public icon list; aborting rather than "
                 "writing the default icon over everything")

    print(f'{card["title"]} — {len(chapters)} chapters, {len(icons)} icons available\n')
    for chapter in chapters:
        title = chapter.get("title", "")
        icon = app.pick_icon(title, icons)
        before = chapter.get("display", {}).get("icon16x16", "(none)")
        print(f'  {title[:52]:<54} {before[-12:]} -> {icon[-12:]}')
        chapter.setdefault("display", {})["icon16x16"] = icon
        # Tracks default to their chapter's icon, but the service sets both, so
        # keep them consistent here too.
        for track in chapter.get("tracks", []):
            track.setdefault("display", {})["icon16x16"] = icon

    if not apply:
        print("\ndry run — pass --apply to write this back")
        return

    # Post the card back whole. Sending a reconstructed subset would drop
    # trackUrl/duration/fileSize and break playback.
    app.yoto("POST", "/content", json={
        "cardId": card["cardId"],
        "title": card["title"],
        "metadata": card.get("metadata", {}),
        "content": card["content"],
    })
    print(f'\nupdated {card["cardId"]}')


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--apply"]
    try:
        if not args:
            print("your cards:")
            list_cards()
        else:
            retrofit(args[0], apply="--apply" in sys.argv)
    except RuntimeError as e:
        # Almost always "not authenticated" -- no token on the /config volume.
        sys.exit(f"{e}\nStart the service and connect your Yoto account first.")
