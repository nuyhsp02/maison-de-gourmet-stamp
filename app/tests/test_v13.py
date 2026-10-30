import json
import math
import re
import xml.etree.ElementTree as ET

from app import ROOT
from conftest import BASE


def luminance(color):
    values = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4 for value in values]
    return sum(a * b for a, b in zip(linear, (.2126, .7152, .0722)))


def test_v13_uses_required_palette_and_high_contrast_completed_ink():
    css = (ROOT / 'static/style.css').read_text()
    tokens = dict(re.findall(r'--([a-z-]+):\s*(#[0-9A-Fa-f]{6})', css))
    assert tokens['mint'] == '#C8D7C3'
    assert tokens['red'] == '#B61219'
    contrast = (luminance(tokens['card']) + .05) / (luminance(tokens['red']) + .05)
    assert contrast >= 4.5
    inactive = re.search(r'\.stamp-seal\s*\{([^}]+)\}', css).group(1)
    active = re.search(r'\.stamp-item.collected \.stamp-seal\s*\{([^}]+)\}', css).group(1)
    assert 'dashed var(--mint)' in inactive
    assert 'background:var(--red)' in active
    assert 'solid var(--red)' in active
    assert 'color:var(--card)' in active
    assert '.stamp-impression' in css and '.stamp-check' in css


def test_registered_placeholders_match_all_ten_product_ids_and_safe_svg_symbols():
    event = json.loads((ROOT / 'event.json').read_text())
    registry = (ROOT / 'static/stamp-art.js').read_text()
    pairs = dict(re.findall(r"'([a-z0-9-]+)': '([a-z0-9-]+)'", registry))
    assert set(pairs) == {p['id'] for p in event['products']}
    svg = ET.fromstring((ROOT / 'static/stamp-art.svg').read_text())
    ns = {'svg': 'http://www.w3.org/2000/svg'}
    symbols = {s.attrib['id']: s for s in svg.findall('.//svg:symbol', ns)}
    assert set(symbols) == set(pairs.values())
    assert len(symbols) == 10
    for symbol in symbols.values():
        assert symbol.attrib['viewBox'] == '0 0 48 48'
        assert symbol.find('svg:g', ns).attrib['stroke'] == 'currentColor'
    for forbidden in ('image', 'script', 'foreignObject', 'text'):
        assert not svg.findall('.//svg:' + forbidden, ns)


def test_artwork_slot_fits_same_ornament_size_in_each_mobile_layout():
    css = (ROOT / 'static/style.css').read_text()
    art = re.search(r'\.stamp-art\s*\{([^}]+)\}', css).group(1)
    assert art.count('var(--ornament-size) * .52') == 2
    assert 'overflow:hidden' in art
    assert 'width:' not in re.search(r'\.stamp-item.collected \.stamp-art\s*\{([^}]+)\}', css).group(1)
    for viewport in (320, 375, 390, 412):
        padding = 12 if viewport <= 360 else 16
        content = viewport - 4 * padding - 2
        diameter = min(57, max(42, viewport * .134))
        # Conservative rotated-square envelope plus badge/shadow allowance.
        ink_envelope = diameter * (math.cos(math.radians(4)) + math.sin(math.radians(4))) + 4
        assert ink_envelope < content / 4
        half_art = diameter * .52 / 2
        # Include the deliberate 2px lift: even a filled square fits inside the ring.
        assert math.hypot(half_art, half_art + 2) <= (diameter - 8) / 2


def test_artwork_registry_loads_before_renderer_and_asset_routes_are_available(app):
    client = app.test_client()
    page = client.get('/', base_url=BASE).text
    assert page.index('/static/stamp-art.js') < page.index('/static/app.js')
    for path in ('/static/stamp-art.js', '/static/stamp-art.svg'):
        response = client.get(path, base_url=BASE)
        assert response.status_code == 200
        assert 'Set-Cookie' not in response.headers
    assert client.get('/api/state', base_url=BASE).json['customer'] is None
