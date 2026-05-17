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
        """Fetches recent messages from a slack channel."""
        if not self.client:
            print("WARNING: SLACK_BOT_TOKEN not found or slack_sdk not installed. Falling back to mock data.")
            return self._mock_fetch(channel_id)
            
        try:
            print(f"Connecting to Slack API for channel {channel_id}...")
            # Call conversations.history
            result = self.client.conversations_history(channel=channel_id, limit=limit)
            
            messages = []
            for message in result.get("messages", []):
                # Ignore subtype messages (like channel joins) and bot messages
                if not message.get("subtype") and not message.get("bot_id"):
                    text = message.get("text", "").strip()
                    if text:
                        messages.append(text)
                        
            # Slack returns newest first, we probably want oldest first to reconstruct threads/context
            messages.reverse()
            return messages
            
        except SlackApiError as e:
            print(f"Slack API Error fetching channel {channel_id}: {e}")
            return self._mock_fetch(channel_id)
        except Exception as e:
            print(f"Error fetching Slack channel {channel_id}: {e}")
            return self._mock_fetch(channel_id)
            
    def _mock_fetch(self, channel_id: str) -> List[str]:
        """Mocks fetching recent messages from a slack channel."""
        return [
            "Hey everyone, quick update from the post-mortem yesterday.",
            "Don't deploy payments after 6 PM because Stripe webhooks fail silently.",
            "Also, whoever is grabbing lunch, let me know.",
            "Make sure to tag #devops in the PR if it touches the DB schema."
        ]
