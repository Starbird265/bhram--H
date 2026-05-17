import os
from typing import Optional
try:
    from notion_client import Client
except ImportError:
    Client = None

class NotionConnector:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("NOTION_API_KEY")
        self.client = Client(auth=self.api_key) if self.api_key and Client else None
        
    def fetch_page(self, page_id: str) -> str:
        """Fetches a Notion page and extracts its textual content."""
        if not self.client:
            print("WARNING: NOTION_API_KEY not found or notion-client not installed. Falling back to mock data.")
            return self._mock_fetch(page_id)
            
        try:
            print(f"Connecting to Notion API for page {page_id}...")
            response = self.client.blocks.children.list(block_id=page_id)
            
            content_lines = []
            for block in response.get("results", []):
                block_type = block.get("type")
                if block_type in ["paragraph", "heading_1", "heading_2", "heading_3", "bulleted_list_item", "numbered_list_item", "quote"]:
                    rich_text = block.get(block_type, {}).get("rich_text", [])
                    line_text = "".join([rt.get("plain_text", "") for rt in rich_text])
                    if not line_text.strip():
                        continue
                        
                    if block_type.startswith("heading"):
                        level = int(block_type.split("_")[1])
                        content_lines.append(f"\n{'#' * level} {line_text}")
                    elif block_type.endswith("list_item"):
                        content_lines.append(f"- {line_text}")
                    elif block_type == "quote":
                        content_lines.append(f"> {line_text}")
                    else:
                        content_lines.append(line_text)
                        
            return "\n".join(content_lines)
            
        except Exception as e:
            print(f"Error fetching Notion page {page_id}: {e}")
            return self._mock_fetch(page_id)
            
    def _mock_fetch(self, page_id: str) -> str:
        """Mocks fetching a Notion page."""
        return """
# Engineering Best Practices

Welcome to the engineering handbook.

## Code Review
All pull requests must be reviewed by at least two senior engineers.
Do not bypass branch protection rules.
If it is a hotfix, one senior engineer is sufficient but it must be tagged [HOTFIX].

## Database Migrations
Always run migrations locally before deploying.
Never delete a column in the same PR that removes the code using it. Deprecate the code first, then drop the column in the next release to avoid downtime.

## Infrastructure
We use AWS for all infrastructure.
Do not manually spin up EC2 instances. Always use the Terraform modules located in the `infrastructure/` repo.
If you manually create an instance, it will be automatically terminated by the security scanner.
"""
