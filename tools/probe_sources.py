"""Ad-hoc check of the new collectors against live feeds (not a unit test)."""

import asyncio

from tgfarm.config import SourceSpec
from tgfarm.s1_sources.base import build_client
from tgfarm.s1_sources.extra_sources import (
    GitHubCollector,
    HackerNewsCollector,
    RedditCollector,
    YouTubeCollector,
)

SPECS = [
    SourceSpec(id="reddit_ai", name="r/MachineLearning", kind="reddit", url="",
               niches=["tech"], params={"subreddit": "MachineLearning"}),
    SourceSpec(id="hn", name="HN", kind="hackernews", url="",
               niches=["tech"], params={"min_points": 150}),
    SourceSpec(id="gh", name="vLLM", kind="github", url="",
               niches=["tech"], params={"owner": "vllm-project", "repo": "vllm"}),
    SourceSpec(id="yt", name="Google", kind="youtube", url="",
               niches=["tech"], params={"channel_id": "UC_x5XG1OV2P6uZZ5FSM9Ttw"}),
]


async def main() -> None:
    collectors = {
        "reddit": RedditCollector(),
        "hackernews": HackerNewsCollector(),
        "github": GitHubCollector(),
        "youtube": YouTubeCollector(),
    }
    async with build_client() as client:
        for spec in SPECS:
            try:
                items = await collectors[spec.kind].fetch(spec, client)
                head = items[0].title[:52] if items else "-"
                print(f"{spec.id:12} {len(items):3d} items | {head}")
                if items:
                    body = items[0].text
                    print(f"{'':12}      text {len(body):5d} chars | {body[:60]!r}")
            except Exception as exc:  # noqa: BLE001
                print(f"{spec.id:12} ERR {type(exc).__name__}: {str(exc)[:90]}")


asyncio.run(main())