line = "- Don't deploy payments after 6 PM because Stripe webhooks fail silently."
lower_line = line.lower()
keywords = ["always", "never", "must", "do not", "don't", "important", "step", "how to", "rule", "make sure", "ensure", "avoid", "critical", "warning", "note"]
for keyword in keywords:
    if keyword in lower_line:
        print(f"Found: {keyword}")

