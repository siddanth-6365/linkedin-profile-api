"""Dev tool: snapshot a real Voyager response and show what is inside it.

    python -m linkedin_profile_api.capture <profile-url> [--out captures/x.json]

This is how the undocumented bits get pinned down. `$type` histograms show which
entities a decoration actually returns and whether a section list came back
truncated, which no amount of reading about LinkedIn's API will tell you.

Captures land in captures/ (gitignored) — they hold real profile data.
"""

import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

from . import config, normalize, urls, voyager


def type_histogram(payloads) -> dict[str, int]:
    """`$type` -> count across every `included` entity, most common first."""
    counter: Counter[str] = Counter()
    for payload in payloads:
        for entity in (payload or {}).get("included") or []:
            if isinstance(entity, dict):
                counter[entity.get("$type") or "(no $type)"] += 1
    return dict(counter.most_common())


async def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    config.load_env()
    slug = urls.public_id(argv[0])
    out = Path(argv[argv.index("--out") + 1]) if "--out" in argv else None

    primary, sections, warnings = await voyager.fetch_raw(slug)

    print(f"\n=== {slug} ===")
    print(f"primary: {len(primary.get('included') or [])} entities")
    for key, payload in sorted(sections.items()):
        items = normalize.section_entities(payload, normalize.merge_index([payload]))
        print(f"  {key:16s} {len(items):3d} items")
    for warning in warnings:
        print(f"  ! {warning}")

    print("\n--- $type histogram (all payloads) ---")
    for type_name, count in type_histogram([primary, *sections.values()]).items():
        print(f"  {count:4d}  {type_name}")

    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"primary": primary, "sections": sections}, indent=2))
        print(f"\nwrote {out}  (real profile data — gitignored)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
