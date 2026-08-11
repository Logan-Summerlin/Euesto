from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
SIZE = 256


def main() -> None:
    image = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((8, 8, 248, 248), radius=58, fill="#4169E1")
    draw.rounded_rectangle((45, 52, 211, 183), radius=38, fill="#F8FAFC")
    draw.polygon([(82, 174), (64, 212), (114, 181)], fill="#F8FAFC")
    for x in (86, 128, 170):
        draw.ellipse((x - 11, 107 - 11, x + 11, 107 + 11), fill="#4169E1")
    draw.ellipse((184, 178, 224, 218), fill="#FF7A59")
    output = ROOT / "assets" / "app.ico"
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(
        output,
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print(output)


if __name__ == "__main__":
    main()
