"""Regenerates the two sample images in sample_notes/photos. Development only.

Needs Pillow (pip install pillow). The app itself never runs this and needs no
packages. The "notebook page" is drawn with a slanted computer font, which is
easier to read than real handwriting: for your demo, replace it with a photo
of a page you wrote by hand (see README).
"""
import os
import random
import sys

from PIL import Image, ImageDraw, ImageFont

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "sample_notes", "photos")
FONTS = "/usr/share/fonts/truetype/dejavu/"


def font(name, size):
    try:
        return ImageFont.truetype(FONTS + name, size)
    except OSError:
        return ImageFont.load_default()


def notebook():
    random.seed(7)
    W, H = 1100, 1250
    page = Image.new("RGB", (W, H), (250, 246, 228))
    d = ImageDraw.Draw(page)
    for y in range(150, H, 62):
        d.line([(0, y), (W, y)], fill=(196, 214, 232), width=2)
    d.line([(120, 0), (120, H)], fill=(232, 170, 170), width=3)
    lines = [
        "Tues 10 Nov - corridor chat w/ Priya",
        "",
        "P wants to LEAD the Helix pilot",
        "said yes in principle !!",
        "",
        "but only if backfill sorted",
        "-> ask Dan, 2 days/wk from Kofi?",
        "",
        "conference: tell her YES",
        "(budget ok - Mei confirmed £600)",
        "",
        "do before 1:1 on the 16th",
    ]
    y = 96
    for text in lines:
        if text:
            layer = Image.new("RGBA", (W, 90), (0, 0, 0, 0))
            ImageDraw.Draw(layer).text((150 + random.randint(-8, 14), 12), text,
                                       font=font("DejaVuSans-Oblique.ttf", 40), fill=(28, 40, 110, 255))
            layer = layer.rotate(random.uniform(-1.6, 1.6), resample=Image.BICUBIC)
            page.paste(layer, (0, y - 8), layer)
        y += 62
    page = page.rotate(-1.2, resample=Image.BICUBIC, fillcolor=(90, 84, 76), expand=True)
    page.save(os.path.join(OUT, "2026-11-10 notebook page.jpg"), quality=88)


def chat():
    W, H = 900, 560
    img = Image.new("RGB", (W, H), (245, 245, 247))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 64], fill=(70, 71, 117))
    d.text((24, 18), "Dan Okafor", font=font("DejaVuSans-Bold.ttf", 24), fill="white")
    d.text((700, 24), "Thu 12 Nov", font=font("DejaVuSans.ttf", 18), fill=(210, 210, 230))
    msgs = [
        ("me", "any chance of backfill for Priya so she can lead the pilot?"),
        ("Dan", "Not full time. Headcount freeze holds until January."),
        ("Dan", "I can give you Kofi 2 days a week from 1 Dec."),
        ("me", "that might be enough. can you confirm by Monday?"),
        ("Dan", "Will confirm Mon 16 Nov after I speak to his manager."),
    ]
    y = 90
    for who, text in msgs:
        mine = who == "me"
        f = font("DejaVuSans.ttf", 21)
        w = int(d.textlength(text, font=f)) + 36
        x = W - w - 24 if mine else 24
        d.text((x + 4, y), "You" if mine else "Dan", font=font("DejaVuSans-Bold.ttf", 15), fill=(110, 110, 120))
        d.rounded_rectangle([x, y + 22, x + w, y + 68], radius=10, fill=(226, 227, 245) if mine else "white",
                            outline=(214, 214, 222))
        d.text((x + 18, y + 33), text, font=f, fill=(30, 30, 36))
        y += 90
    img.save(os.path.join(OUT, "2026-11-12 chat with dan.png"))


if __name__ == "__main__":
    notebook()
    chat()
    print("wrote sample images to", os.path.abspath(OUT))
