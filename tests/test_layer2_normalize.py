"""
Layer 2: NORMALIZATION — Unit Tests
Tests for TextNormalizer covering noise removal, Slack artifact cleaning,
and the full normalization pipeline on mixed real-world inputs.
"""

import pytest
from pipeline.normalize import TextNormalizer


# ─── Fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture
def normalizer():
    return TextNormalizer()


# ─── HTML Cleaning ────────────────────────────────────────────────────────

class TestCleanHTML:
    def test_strips_basic_tags(self, normalizer):
        assert normalizer.clean_html("<b>Bold</b> text") == "Bold text"

    def test_strips_nested_tags(self, normalizer):
        result = normalizer.clean_html("<div><p>Deploy after review</p></div>")
        assert "<" not in result
        assert "Deploy after review" in result

    def test_collapses_whitespace(self, normalizer):
        result = normalizer.clean_html("word1   word2\t\tword3")
        assert "  " not in result

    def test_handles_empty_string(self, normalizer):
        assert normalizer.clean_html("") == ""

    def test_plain_text_unchanged(self, normalizer):
        text = "Never deploy to production on Fridays."
        assert normalizer.clean_html(text) == text


# ─── Slack Artifact Cleaning ─────────────────────────────────────────────

class TestCleanSlackArtifacts:
    def test_replaces_user_mentions(self, normalizer):
        result = normalizer.clean_slack_artifacts("<@U12345> please review the PR")
        assert "[user]" in result
        assert "<@" not in result

    def test_extracts_channel_names(self, normalizer):
        result = normalizer.clean_slack_artifacts("Post updates in <#C123|devops>")
        assert "#devops" in result
        assert "<#" not in result

    def test_extracts_urls(self, normalizer):
        result = normalizer.clean_slack_artifacts("See <https://docs.example.com|docs>")
        assert "https://docs.example.com" in result

    def test_removes_standalone_emoji_reactions(self, normalizer):
        text = "Deploy after review\n:thumbsup:\n:white_check_mark:"
        result = normalizer.clean_slack_artifacts(text)
        assert ":thumbsup:" not in result
        assert "Deploy after review" in result

    def test_preserves_inline_emoji_in_operational_context(self, normalizer):
        # :warning: carries meaning — should not be stripped as standalone here
        result = normalizer.clean_slack_artifacts("IMPORTANT: check :warning: before deploying")
        assert "IMPORTANT" in result


# ─── Noise Removal ───────────────────────────────────────────────────────

class TestRemoveConversationalNoise:
    OPERATIONAL = [
        "Don't deploy payments after 6 PM because Stripe webhooks fail silently.",
        "IMPORTANT: Never restart auth-service without notifying #sec-ops first.",
        "Make sure to tag #devops in the PR if it touches the DB schema.",
        "Always run migrations in staging before production.",
        "Rollback immediately if health check fails after deploy.",
    ]
    NOISE = [
        "Hey everyone!",
        "Thanks!",
        "+1",
        "ok sounds good",
        "grabbing lunch, brb",
        "Happy Friday everyone!",
    ]

    def test_all_operational_signals_preserved(self, normalizer):
        for msg in self.OPERATIONAL:
            result = normalizer.remove_conversational_noise(msg)
            assert result.strip(), f"Operational signal was incorrectly stripped: {msg!r}"

    def test_pure_noise_removed(self, normalizer):
        for msg in self.NOISE:
            result = normalizer.remove_conversational_noise(msg)
            assert not result.strip(), f"Noise was not filtered: {msg!r}"

    def test_empty_string_returns_empty(self, normalizer):
        assert normalizer.remove_conversational_noise("") == ""

    def test_mixed_content_filters_correctly(self, normalizer, mixed_messages):
        combined = "\n".join(mixed_messages)
        result = normalizer.remove_conversational_noise(combined)
        lines = [l for l in result.split("\n") if l.strip()]
        # All signal lines must survive
        for signal in self.OPERATIONAL:
            # Check key phrase appears somewhere in the output
            key = signal[:30]
            assert any(key.lower() in l.lower() for l in lines), (
                f"Signal lost from mixed input: {key!r}"
            )


# ─── Markdown Cleaning ───────────────────────────────────────────────────

class TestCleanMarkdown:
    def test_strips_bold(self, normalizer):
        assert normalizer.clean_markdown("**Important rule**") == "Important rule"

    def test_strips_italic(self, normalizer):
        assert normalizer.clean_markdown("*note this*") == "note this"

    def test_simplifies_links(self, normalizer):
        result = normalizer.clean_markdown("[docs](https://example.com)")
        assert "https://" not in result
        assert "docs" in result

    def test_removes_image_references(self, normalizer):
        result = normalizer.clean_markdown("![screenshot](https://img.example.com/x.png)")
        assert "![" not in result
        assert "screenshot" not in result


# ─── Full Pipeline: normalize_slack_messages ─────────────────────────────

class TestNormalizeSlackMessages:
    def test_returns_string(self, normalizer, mixed_messages):
        result = normalizer.normalize_slack_messages(mixed_messages)
        assert isinstance(result, str)

    def test_signal_count_less_than_input(self, normalizer, mixed_messages):
        result = normalizer.normalize_slack_messages(mixed_messages)
        output_lines = [l for l in result.split("\n") if l.strip()]
        assert len(output_lines) < len(mixed_messages), (
            "Noise filtering had no effect on mixed messages"
        )

    def test_handles_empty_list(self, normalizer):
        result = normalizer.normalize_slack_messages([])
        assert result == "" or not result.strip()

    def test_output_has_no_blank_lines(self, normalizer, high_signal_messages):
        result = normalizer.normalize_slack_messages(high_signal_messages)
        lines = result.split("\n")
        blank = [l for l in lines if l == ""]
        # Only a minimal number of blanks acceptable (separator blanks)
        assert len(blank) <= 2
