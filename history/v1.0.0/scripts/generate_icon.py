"""Draw MMO's code-native film-frame application mark."""
from pathlib import Path
from PIL import Image, ImageDraw

root = Path(__file__).resolve().parents[1]
image = Image.new('RGBA', (256, 256), '#0D131D')
draw = ImageDraw.Draw(image)
draw.rounded_rectangle((20, 34, 236, 222), radius=28, outline='#48B7FF', width=11)
for x in (36, 204):
    for y in (55, 91, 127, 163, 199):
        draw.rounded_rectangle((x, y, x+15, y+13), radius=3, fill='#48B7FF')
draw.line([(74,177),(74,84),(128,145),(182,84),(182,177)], fill='#EAF2FA', width=17, joint='curve')
assets = root / 'assets'
assets.mkdir(exist_ok=True)
image.save(assets / 'MMO.png')
image.save(assets / 'MMO.ico', sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])
