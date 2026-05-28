"""
Layer 2: NORMALIZATION
Cleans, standardizes, and format-unifies raw text from any source.
Strips conversational noise, pleasantries, HTML, excessive markdown, 
and Slack-specific artifacts to prepare clean text for chunking.
"""

import re
from typing import List


class TextNormalizer:
    """
    Multi-stage text normalizer that progressively cleans raw input.
    
    Pipeline:
      1. Strip HTML tags
      2. Normalize Slack-specific artifacts (user mentions, emoji, reactions)
      3. Remove conversational noise (pleasantries, off-topic chatter)
      4. Clean markdown formatting artifacts
      5. Normalize whitespace
    """

    # Patterns that indicate conversational noise (not operational knowledge)
    NOISE_PATTERNS = [
        r"(?i)^[-•*]?\s*(hey|hi|hello|good morning|good afternoon|good evening|gm|yo)\b.*$",
        r"(?i)^[-•*]?\s*(thanks|thank you|thx|ty|cheers|great job|nice|cool|awesome|lol|haha|lmao)\b.*$",
        r"(?i)^[-•*]?\s*(brb|gtg|afk|bbl|ttyl)\b.*$",
        r"(?i)^[-•*]?\s*(grabbing|getting|going for)\s+(lunch|coffee|food|break).*$",
        r"(?i)^[-•*]?\s*\+1\s*$",
        r"(?i)^[-•*]?\s*(ok|okay|sure|got it|sounds good|ok sounds good|will do|done|noted)\s*\.?\s*$",
        r"(?i)^[-•*]?\s*(who wants|anyone want|let me know if|whoever is)\s+(lunch|coffee|food|grabbing|getting).*$",
        r"(?i)^[-•*]?\s*(also,?\s*)?(whoever|anyone).*\b(lunch|coffee|food|break)\b.*$",
        r"(?i)^[-•*]?\s*happy (birthday|friday|monday|weekend).*$",
        r"(?i)^[-•*]?\s*(see you|catch you|talk to you).*$",
    ]

    # Slack-specific artifact patterns
    SLACK_PATTERNS = {
        "user_mention": re.compile(r"<@[A-Z0-9]+>"),          # <@U12345>
        "channel_ref": re.compile(r"<#[A-Z0-9]+\|([^>]+)>"),  # <#C123|channel-name>
        "url": re.compile(r"<(https?://[^|>]+)\|?[^>]*>"),    # <http://url|display>
        "emoji": re.compile(r":([a-z0-9_+-]+):"),              # :thumbsup:
        "reaction": re.compile(r"^\s*:[a-z0-9_+-]+:\s*$"),     # Standalone emoji reactions
    }

    @staticmethod
    def clean_html(raw_html: str) -> str:
        """Strips HTML tags and standardizes whitespace."""
        cleanr = re.compile('<.*?>')
        cleantext = re.sub(cleanr, '', raw_html)
        # normalize horizontal whitespace
        cleantext = re.sub(r'[ \t]+', ' ', cleantext)
        return cleantext.strip()

    @staticmethod
    def clean_slack_artifacts(text: str) -> str:
        """Normalizes Slack-specific formatting into plain text."""
        # Replace user mentions with placeholder
        text = TextNormalizer.SLACK_PATTERNS["user_mention"].sub("[user]", text)
        # Extract channel names from references
        text = TextNormalizer.SLACK_PATTERNS["channel_ref"].sub(r"#\1", text)
        # Extract URLs from Slack link format
        text = TextNormalizer.SLACK_PATTERNS["url"].sub(r"\1", text)
        # Remove standalone emoji reactions (lines that are just :emoji:)
        lines = text.split("\n")
        lines = [l for l in lines if not TextNormalizer.SLACK_PATTERNS["reaction"].match(l)]
        text = "\n".join(lines)
        # Keep inline emoji as-is (they sometimes carry meaning like :warning:)
        return text

    @staticmethod
    def remove_conversational_noise(text: str) -> str:
        """
        Removes lines that are pure conversational noise.
        Preserves lines that contain operational signals even if they 
        start with pleasantries (e.g. 'Hey, don't deploy after 6pm').
        """
        # Operational signal keywords — if present, keep the line regardless
        operational_keywords = [
            "always", "never", "must", "do not", "don't", "important",
            "step", "how to", "rule", "make sure", "ensure", "avoid",
            "critical", "warning", "note:", "deploy", "production",
            "database", "schema", "migration", "security", "approval",
            "escalate", "hotfix", "rollback", "incident", "outage",
            "budget", "deadline", "compliance", "audit", "policy",
        ]

        lines = text.split("\n")
        clean_lines = []

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue

            # Check if line contains any operational signal
            lower_line = stripped.lower()
            has_signal = any(kw in lower_line for kw in operational_keywords)

            if has_signal:
                clean_lines.append(stripped)
                continue

            # Check if line matches pure noise patterns
            is_noise = any(re.match(pattern, stripped) for pattern in TextNormalizer.NOISE_PATTERNS)
            if not is_noise:
                clean_lines.append(stripped)

        return "\n".join(clean_lines)

    @staticmethod
    def clean_markdown(raw_md: str) -> str:
        """Removes excessive markdown artifacts for cleaner distillation."""
        # Remove image references FIRST (before link simplifier strips [...](url))
        text = re.sub(r'!\[.*?\]\(.*?\)', '', raw_md)
        # Remove bold/italic markers
        text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)
        text = re.sub(r'\*(.*?)\*', r'\1', text)
        # Simplify links to just anchor text
        text = re.sub(r'\[(.*?)\]\(.*?\)', r'\1', text)
        return text.strip()

    @staticmethod
    def normalize_whitespace(text: str) -> str:
        """Collapses excessive blank lines and trailing spaces."""
        # Collapse 3+ newlines into 2
        text = re.sub(r'\n{3,}', '\n\n', text)
        # Remove trailing spaces on each line
        text = re.sub(r'[ \t]+$', '', text, flags=re.MULTILINE)
        return text.strip()

    @staticmethod
    def normalize_document(content: str) -> str:
        """
        Runs the full Layer 2 normalization pipeline:
          1. HTML → plain text
          2. Slack artifacts → normalized
          3. Conversational noise → removed
          4. Markdown artifacts → simplified
          5. Whitespace → normalized
        """
        cleaned = TextNormalizer.clean_html(content)
        cleaned = TextNormalizer.clean_slack_artifacts(cleaned)
        cleaned = TextNormalizer.remove_conversational_noise(cleaned)
        cleaned = TextNormalizer.clean_markdown(cleaned)
        cleaned = TextNormalizer.normalize_whitespace(cleaned)
        return cleaned

    @staticmethod
    def normalize_slack_messages(messages: List[str]) -> str:
        """
        Specialized normalizer for a list of Slack messages.
        Preserves message boundaries with bullet markers for chunking.
        """
        formatted = []
        for msg in messages:
            cleaned = TextNormalizer.clean_slack_artifacts(msg)
            cleaned = cleaned.strip()
            if cleaned:
                formatted.append(f"- {cleaned}")

        combined = "\n".join(formatted)
        # Now run the full pipeline on the combined text
        return TextNormalizer.remove_conversational_noise(combined)
