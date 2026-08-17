"""Generate icon.ico for the desktop build using PIL."""
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw
except ImportError:
    print("Missing Pillow. Install with:")
    print("  pip install pillow")
    sys.exit(1)

ROOT = Path(__file__).parent
ico_path = ROOT / "icon.ico"

print(f"Generating {ico_path.name}...")

# Create a 256x256 icon with ASMR headphone theme
size = 256
img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
draw = ImageDraw.Draw(img)

# Background circle - warm earthy gradient approximation
center = size // 2
radius = 120
for r in range(radius, 0, -1):
    # Gradient from #5C3D2E to #2B1D14
    factor = r / radius
    color_r = int(92 * factor + 43 * (1 - factor))
    color_g = int(61 * factor + 29 * (1 - factor))
    color_b = int(46 * factor + 20 * (1 - factor))
    draw.ellipse(
        [center - r, center - r, center + r, center + r],
        fill=(color_r, color_g, color_b, 255)
    )

# Headphone band - arc at top
band_color = (245, 239, 230, 230)  # #F5EFE6
draw.arc([70, 50, 186, 166], 180, 360, fill=band_color, width=12)

# Left ear cup
draw.rounded_rectangle([50, 115, 85, 165], radius=8, fill=(245, 239, 230, 230))
draw.rounded_rectangle([55, 120, 80, 160], radius=6, fill=(43, 29, 20, 128))

# Right ear cup
draw.rounded_rectangle([171, 115, 206, 165], radius=8, fill=(245, 239, 230, 230))
draw.rounded_rectangle([176, 120, 201, 160], radius=6, fill=(43, 29, 20, 128))

# Sound waves - left side
wave_color = (245, 239, 230, 150)
draw.arc([22, 118, 38, 138], 90, 270, fill=wave_color, width=3)
draw.arc([10, 108, 30, 148], 90, 270, fill=wave_color, width=2)

# Sound waves - right side
draw.arc([218, 118, 234, 138], 270, 450, fill=wave_color, width=3)
draw.arc([226, 108, 246, 148], 270, 450, fill=wave_color, width=2)

# Play button at bottom
draw.ellipse([106, 163, 150, 207], fill=(185, 100, 32, 230))  # #B96420
# Triangle play icon
play_points = [(122, 177), (122, 193), (136, 185)]
draw.polygon(play_points, fill=(245, 239, 230, 255))

# Generate multiple sizes for better icon quality
sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
images = [img.resize((w, h), Image.Resampling.LANCZOS) for w, h in sizes[1:]]

img.save(ico_path, format='ICO', sizes=sizes, append_images=images)

print(f"Created {ico_path}")
print(f"  Size: {ico_path.stat().st_size / 1024:.1f} KB")
print(f"  Resolutions: {', '.join(f'{w}x{h}' for w, h in sizes)}")
print("\nRun build_desktop.py to use this icon in the exe.")
