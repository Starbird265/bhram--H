import os
from typing import List, Optional

try:
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError
except ImportError:
    WebClient = None
    SlackApiError = Exception


class SlackConnector:
    def __init__(self, bot_token: Optional[str] = None):
        self.bot_token = bot_token or os.getenv("SLACK_BOT_TOKEN")
        self.client = WebClient(token=self.bot_token) if self.bot_token and WebClient else None

    def fetch_channel_history(self, channel_id: str, limit: int = 50) -> List[str]:
        """Fetches recent messages from a Slack channel.

        Returns [] (not mock data) when credentials are missing,
        so the pipeline doesn't silently ingest fake knowledge.
        """
        if not self.client:
            print(
                "  [Slack] No credentials — skipping. "
                "Connect via /api/connectors/slack/connect to ingest real messages."
            )
            return []

        try:
            print(f"  [Slack] Fetching #{channel_id} (limit={limit})...")
            result = self.client.conversations_history(channel=channel_id, limit=limit)

            messages = []
            for message in result.get("messages", []):
                # Skip subtypes (channel joins, etc.) and bot messages
                if not message.get("subtype") and not message.get("bot_id"):
                    text = message.get("text", "").strip()
                    if text:
                        messages.append(text)

            # Slack returns newest first; reverse so context reads chronologically
            messages.reverse()
            print(f"  [Slack] Got {len(messages)} real messages from #{channel_id}")
            return messages

        except SlackApiError as e:
            print(f"  [Slack] API error on #{channel_id}: {e.response['error']}")
            return []
        except Exception as e:
            print(f"  [Slack] Unexpected error on #{channel_id}: {e}")
            return []
