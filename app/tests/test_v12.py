import hashlib
import importlib.util
import json
import re
from pathlib import Path

import pytest

from app import ROOT, create_app, load_event, timestamp
from conftest import BASE

spec = importlib.util.spec_from_file_location("mobile_test_setup", ROOT / "tools" / "prepare_mobile_test.py")
mobile_setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mobile_setup)


def test_brand_tree_markup_uses_single_ten_product_list(app):
    html = app.test_client().get("/", base_url=BASE).text
    assert "Maison de Gourmet" in html
    assert "CHRISTMAS STAMP EVENT" in html
    assert html.count('id="stamp-tree"') == 1
    assert 'id="season-grid"' not in html and 'id="select-grid"' not in html
    assert 'aria-label="상품별 스탬프 트리' in html
    assert '/ 10 <small>collected</small>' in html
    assert html.count('id="redeem-button"') == 1
    assert 'id="viewfinder"' in html
    script = (ROOT / 'static/app.js').read_text()
    referenced = set(re.findall(r"\$\('([^']+)'\)", script))
    present = set(re.findall(r'id="([^"]+)"', html))
    assert referenced <= present, referenced - present


def test_tree_positions_are_one_two_three_four_without_horizontal_circle_overlap():
    # Static layout contract, not a substitute for an actual browser layout test.
    css = (ROOT / "static/style.css").read_text()
    rules = re.findall(r"\.stamp-item:nth-child\((\d+)\)\s*\{\s*grid-column:(\d+) / span 2; grid-row:(\d+);", css)
    actual = [(int(index), int(column), int(row)) for index, column, row in rules]
    assert actual == [(1, 4, 1), (2, 3, 2), (3, 5, 2), (4, 2, 3), (5, 4, 3),
                      (6, 6, 3), (7, 1, 4), (8, 3, 4), (9, 5, 4), (10, 7, 4)]
    assert [sum(row == r for _, _, row in actual) for r in range(1, 5)] == [1, 2, 3, 4]
    for viewport in (320, 375, 390, 412):
        padding = 12 if viewport <= 360 else 16
        content = viewport - padding * 2 - 2 - padding * 2
        diameter = min(57, max(42, viewport * .134))
        slot = content / 4
        assert slot >= diameter + 4, viewport
        for _, column, _ in actual:
            center = column * content / 8
            assert center - diameter / 2 >= 0
            assert center + diameter / 2 <= content


def test_mobile_controls_and_scanner_coordinate_css_contract():
    css = (ROOT / "static/style.css").read_text()
    assert '+EOF' not in css
    assert '.button { width:100%; min-height:52px;' in css
    assert '.text-button { min-height:44px;' in css
    assert 'width:44px; height:44px;' in css
    assert 'env(safe-area-inset-bottom)' in css
    assert 'prefers-reduced-motion:reduce' in css
    assert 'white-space:nowrap; text-overflow:ellipsis;' in css
    video = re.search(r"\.camera-view video\s*\{([^}]+)\}", css).group(1)
    for declaration in ('object-fit:cover;', 'object-position:50% 50%;', 'transform:none;', 'position:absolute;'):
        assert declaration in video
    assert 'inset:18%; border:2px' in css


def test_mobile_preparation_preserves_source_products_and_never_creates_database(tmp_path):
    source = tmp_path / "original.json"
    source.write_bytes((ROOT / "event.json").read_bytes())
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    target = tmp_path / "mobile test"
    result = mobile_setup.prepare(source, target, "https://192.168.0.20:8443")
    assert result == target
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    original = json.loads(source.read_text())
    event = load_event(target / "event.json")
    for key in ('id', 'products', 'reward', 'cafe_name', 'title', 'description'):
        assert event[key] == original[key]
    assert event['draft'] is False
    assert 0 < timestamp(event['ends_at']) - timestamp(event['starts_at']) <= 32 * 86400
    assert timestamp(event['reward_until']) > timestamp(event['ends_at'])
    assert sorted(p.name for p in target.iterdir()) == ['Caddyfile', 'RUN.md', 'event.json']
    caddy = (target / 'Caddyfile').read_text()
    for part in ('tls internal', 'skip_install_trust', 'admin off', 'auto_https disable_redirects',
                 '127.0.0.1:8875', 'https://192.168.0.20:8443'):
        assert part in caddy
    run = (target / 'RUN.md').read_text()
    assert str(target / 'collection.sqlite') in run
    assert 'root.crt' in run and '--port 8875' in run


def test_preparation_never_overwrites_an_existing_directory(tmp_path):
    target = tmp_path / 'existing'
    target.mkdir()
    marker = target / 'keep.txt'
    marker.write_text('unchanged')
    with pytest.raises(FileExistsError):
        mobile_setup.prepare(ROOT / 'event.json', target, 'https://192.168.0.20:8443')
    assert marker.read_text() == 'unchanged'
    assert list(target.iterdir()) == [marker]


@pytest.mark.parametrize('origin', [
    'http://192.168.0.20:8443', 'https://127.0.0.1:8443', 'https://localhost:8443',
    'https://0.0.0.0:8443', 'https://192.168.0.20', 'https://192.168.0.20:8443/path',
    'https://user:password@192.168.0.20:8443', 'https://192.168.0.20:8443\ninvalid',
])
def test_invalid_mobile_origin_does_not_write_files(tmp_path, origin):
    destination = tmp_path / 'not-created'
    with pytest.raises(ValueError):
        mobile_setup.prepare(ROOT / 'event.json', destination, origin)
    assert not destination.exists()


def test_generated_config_starts_unchanged_https_app_with_isolated_database(tmp_path):
    target = mobile_setup.prepare(ROOT / 'event.json', tmp_path / 'test', 'https://192.168.0.20:8443')
    app = create_app(target / 'event.json', target / 'collection.sqlite', 'https://192.168.0.20:8443', testing=True)
    response = app.test_client().get('/api/state', base_url='https://192.168.0.20:8443')
    assert response.status_code == 200 and response.json['customer'] is None
    assert len(response.json['event']['products']) == 10
    assert response.json['phase'] == 'running'
