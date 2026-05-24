import asyncio
from datetime import datetime, timezone

from dotenv import load_dotenv

from graphiti_core import Graphiti
from graphiti_core.nodes import EpisodeType

# -----------------------------------
# Load ENV
# -----------------------------------

load_dotenv()

# -----------------------------------
# Graphiti Setup
# -----------------------------------

graphiti = Graphiti(
    "bolt://127.0.0.1:7687",
    "neo4j",
    "Ajay613@"
)

# -----------------------------------
# Sample Story
# -----------------------------------

USER_STORY = """
Feature: Bundle Purchase

Flow 1:
User logs into the application using Login API.

Flow 2:
User selects recharge bundle using Recharge API.

Business Rule:
Bundle limit should be less than or equal to 10.

Flow 3:
User confirms purchase successfully.
"""

# -----------------------------------
# Main
# -----------------------------------

async def main():

    print("Building graph schema...")

    await graphiti.build_indices_and_constraints()

    print("Adding story episode...")

    await graphiti.add_episode(
        name="Bundle Purchase Story V1",
        episode_body=USER_STORY,
        source=EpisodeType.text,
        source_description="user_story",
        reference_time=datetime.now(timezone.utc)
    )

    print("Story stored successfully!")

    await graphiti.close()

# -----------------------------------

if __name__ == "__main__":
    asyncio.run(main())