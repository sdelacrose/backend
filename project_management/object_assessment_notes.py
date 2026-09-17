import html


def plain_text_to_tiptap_json(text):
    normalized_text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized_text.split("\n")

    content = []
    for line in lines:
        if line:
            content.append(
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": line}],
                }
            )
        else:
            content.append({"type": "paragraph"})

    return {
        "type": "doc",
        "content": content or [{"type": "paragraph"}],
    }


def plain_text_to_html(text):
    normalized_text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized_text.split("\n")
    paragraphs = [f"<p>{html.escape(line)}</p>" for line in lines]
    return "".join(paragraphs) or "<p></p>"
