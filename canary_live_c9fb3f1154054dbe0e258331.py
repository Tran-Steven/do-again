def canonical_label(text):
    if not text.isascii():
        raise ValueError("canonical_label requires ASCII input")
    return "-".join(text.lower().split())
