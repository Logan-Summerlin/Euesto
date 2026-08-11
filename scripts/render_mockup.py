"""Render a deterministic README preview when a Qt display is unavailable."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
WIDTH, HEIGHT = 1120, 760


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    return ImageFont.truetype(name, size)


def rounded(draw: ImageDraw.ImageDraw, box, radius: int, fill: str, outline=None) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=1)


def main() -> None:
    image = Image.new("RGB", (WIDTH, HEIGHT), "#11151d")
    draw = ImageDraw.Draw(image)
    sidebar_width = 245

    draw.rectangle((0, 0, sidebar_width, HEIGHT), fill="#0d1118")
    draw.line((sidebar_width, 0, sidebar_width, HEIGHT), fill="#252c38")
    draw.text((18, 20), "OpenRouter Chat", fill="#f1f5f9", font=font(21, True))
    rounded(draw, (14, 60, 230, 102), 8, "#557fe8")
    draw.text((31, 71), "+  New chat", fill="white", font=font(15, True))
    rounded(draw, (13, 121, 231, 166), 8, "#25355e")
    draw.text((25, 133), "Planning a local chatbot", fill="#dce6ff", font=font(14))
    draw.text((21, 710), "Rename", fill="#cbd5e1", font=font(13))
    draw.text((137, 710), "Delete", fill="#ff8b85", font=font(13))

    draw.rectangle((sidebar_width + 1, 0, WIDTH, 62), fill="#151a23")
    draw.line((sidebar_width, 62, WIDTH, 62), fill="#252c38")
    draw.text((267, 22), "Model", fill="#aab4c5", font=font(13))
    rounded(draw, (315, 13, 690, 49), 7, "#1b222e", "#323b4b")
    draw.text((328, 23), "openrouter/auto", fill="#e7eaf0", font=font(13))
    for label, left, right in (
        ("System prompt", 715, 836),
        ("Light", 849, 913),
        ("API key", 926, 1002),
    ):
        rounded(draw, (left, 13, right, 49), 7, "#1b222e", "#323b4b")
        draw.text((left + 10, 23), label, fill="#e7eaf0", font=font(12))

    rounded(draw, (282, 100, 1069, 198), 11, "#1d2a45", "#293d66")
    draw.text((299, 116), "You", fill="#aab4c5", font=font(13, True))
    draw.text((1008, 116), "Copy", fill="#9aa7ba", font=font(11))
    draw.text(
        (299, 151),
        "Build me a fast, minimalist desktop chatbot for OpenRouter.",
        fill="#eef2ff",
        font=font(15),
    )

    rounded(draw, (282, 217, 1069, 413), 11, "#181e28", "#2a3240")
    draw.text((299, 233), "Assistant", fill="#aab4c5", font=font(13, True))
    draw.text((1008, 233), "Copy", fill="#9aa7ba", font=font(11))
    draw.text(
        (299, 270),
        "Absolutely. The app can stay native and lightweight while streaming",
        fill="#e7eaf0",
        font=font(15),
    )
    draw.text(
        (299, 297),
        "responses, keeping chats local, and storing the API key in Windows",
        fill="#e7eaf0",
        font=font(15),
    )
    draw.text((299, 324), "Credential Manager.", fill="#e7eaf0", font=font(15))
    rounded(draw, (299, 355, 540, 397), 6, "#111827")
    draw.text((315, 367), "python app.py", fill="#e5e7eb", font=font(14))

    draw.rectangle((245, 618, WIDTH, HEIGHT), fill="#151a23")
    draw.line((245, 618, WIDTH, 618), fill="#252c38")
    rounded(draw, (274, 638, 1080, 699), 8, "#151a23", "#323b4b")
    draw.text((291, 658), "Message...  (Ctrl+Enter to send)", fill="#727d8e", font=font(14))
    draw.text((275, 722), "62 billed tokens  ·  $0.00042", fill="#929dad", font=font(12))
    rounded(draw, (987, 710, 1080, 746), 7, "#557fe8")
    draw.text((1015, 720), "Send", fill="white", font=font(13, True))

    output = ROOT / "assets" / "screenshot.png"
    image.save(output, optimize=True)
    print(output)


if __name__ == "__main__":
    main()
