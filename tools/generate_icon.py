from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "LEDERG-CRMP-Avatar.ico"
AVATAR_OUTPUT = ROOT / "LEDERG-CRMP-Avatar.png"
COVER_OUTPUT = ROOT / "LEDERG-CRMP-Cover.png"
SIZE = 512


def make_icon() -> Image.Image:
    image = Image.new("RGBA", (SIZE, SIZE), (5, 5, 8, 255))
    glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((82, 74, 444, 440), fill=(255, 24, 65, 80))
    image = Image.alpha_composite(image, glow.filter(ImageFilter.GaussianBlur(52)))

    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (22, 22, 490, 490), radius=112, fill=(13, 13, 18, 255),
        outline=(64, 37, 48, 255), width=5,
    )
    draw.rounded_rectangle((34, 34, 478, 478), radius=98,
                           outline=(255, 255, 255, 15), width=2)
    draw.polygon(
        [(122, 112), (205, 112), (205, 302), (354, 302), (354, 390), (122, 390)],
        fill=(245, 245, 248, 255),
    )
    slash_glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    ImageDraw.Draw(slash_glow).polygon(
        [(321, 78), (409, 78), (238, 432), (150, 432)],
        fill=(255, 25, 67, 170),
    )
    image = Image.alpha_composite(image, slash_glow.filter(ImageFilter.GaussianBlur(16)))
    draw = ImageDraw.Draw(image)
    draw.polygon(
        [(321, 78), (409, 78), (238, 432), (150, 432)],
        fill=(255, 43, 77, 255),
    )
    draw.line((57, 84, 57, 56, 87, 56), fill=(255, 43, 77, 235), width=5)
    draw.line((425, 456, 455, 456, 455, 426), fill=(255, 43, 77, 235), width=5)
    return image


def load_font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    candidates = (
        Path("C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf"),
        Path("C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
             else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    )
    for candidate in candidates:
        if candidate.is_file():
            return ImageFont.truetype(str(candidate), size=size)
    return ImageFont.load_default()


def make_cover(icon: Image.Image) -> Image.Image:
    width, height = 1600, 900
    image = Image.new("RGB", (width, height), (4, 6, 11))
    pixels = image.load()
    for y in range(height):
        for x in range(width):
            red = int(4 + 13 * (1 - x / width) + 7 * y / height)
            blue = int(9 + 10 * x / width)
            pixels[x, y] = (red, 5, blue)

    glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((430, -280, 1420, 690), fill=(214, 0, 42, 145))
    gd.ellipse((-220, 270, 640, 960), fill=(100, 0, 25, 110))
    image = Image.alpha_composite(image.convert("RGBA"), glow.filter(ImageFilter.GaussianBlur(125)))

    # A stylized skyline and red-lit building windows in the background.
    skyline = Image.new("RGBA", image.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(skyline)
    buildings = [
        (0, 400, 115, 650), (92, 330, 218, 680), (202, 435, 290, 670),
        (280, 300, 378, 690), (365, 370, 470, 680), (468, 252, 580, 700),
        (566, 395, 670, 690), (664, 305, 770, 690), (780, 420, 900, 690),
        (890, 285, 1010, 695), (1005, 385, 1110, 690), (1110, 245, 1240, 700),
        (1230, 350, 1340, 685), (1330, 290, 1455, 700), (1450, 410, 1600, 680),
    ]
    for index, (x1, y1, x2, y2) in enumerate(buildings):
        sd.rectangle((x1, y1, x2, y2), fill=(7, 8, 16, 238))
        for wy in range(y1 + 19, y2 - 12, 24):
            for wx in range(x1 + 13, x2 - 8, 20):
                if (wx * 3 + wy * 5 + index) % 7 in (0, 1, 3):
                    sd.rectangle((wx, wy, wx + 5, wy + 8), fill=(255, 30, 65, 115))
    image = Image.alpha_composite(image, skyline)

    # Angled glass-like overlays and restrained red dividers.
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.polygon([(0, 0), (550, 0), (255, 900), (0, 900)], fill=(0, 0, 0, 75))
    od.polygon([(1240, 0), (1600, 0), (1600, 900), (1010, 900)], fill=(0, 0, 0, 75))
    od.line((0, 155, 1600, 155), fill=(255, 33, 67, 160), width=2)
    od.line((0, 820, 1600, 820), fill=(255, 33, 67, 180), width=2)
    image = Image.alpha_composite(image, overlay)

    # Signature monogram and release cover typography.
    mark = icon.resize((350, 350), Image.Resampling.LANCZOS)
    image.alpha_composite(mark, (74, 240))
    draw = ImageDraw.Draw(image)
    small = load_font(25)
    label = load_font(26, bold=True)
    title = load_font(112, bold=True)
    subtitle = load_font(92, bold=True)
    feature = load_font(25)
    tagline = load_font(30, bold=True)

    draw.text((72, 55), "LEDERG / OFFICIAL RELEASE", font=label, fill=(255, 48, 83, 255))
    draw.text((480, 250), "LEDERG", font=title, fill=(245, 247, 251, 255), stroke_width=1)
    draw.text((487, 370), "CR:MP", font=subtitle, fill=(255, 37, 74, 255), stroke_width=1)
    draw.text((492, 490), "CRIMINAL RUSSIA MULTIPLAYER", font=label, fill=(194, 204, 220, 255))
    draw.line((492, 543, 1070, 543), fill=(255, 37, 74, 230), width=4)
    draw.text((492, 576), "BLACK. RED. NO LIMITS.", font=tagline, fill=(246, 246, 249, 255))
    draw.text((492, 635), "SMART CLIENT UPDATES  /  FILE INTEGRITY  /  WINDOWS x64",
              font=feature, fill=(159, 172, 192, 255))
    draw.text((72, 842), "LEDERG CR:MP LAUNCHER", font=small, fill=(206, 211, 223, 255))
    draw.text((1250, 842), "V2.5.6  •  OFFICIAL", font=label, fill=(255, 52, 86, 255))
    draw.rounded_rectangle((1380, 65, 1525, 105), radius=16,
                           fill=(24, 16, 24, 230), outline=(255, 37, 74, 170), width=1)
    draw.text((1402, 73), "LIVE", font=small, fill=(255, 62, 94, 255))
    return image.convert("RGB")


def main() -> None:
    icon = make_icon()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    icon.save(AVATAR_OUTPUT, format="PNG", optimize=True)
    icon.save(
        OUTPUT,
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    make_cover(icon).save(COVER_OUTPUT, format="PNG", optimize=True)
    print(f"Generated {OUTPUT}, {AVATAR_OUTPUT}, and {COVER_OUTPUT}")


if __name__ == "__main__":
    main()
