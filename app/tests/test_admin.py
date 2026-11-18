"""Staff-only card management, with synthetic credentials and isolated databases."""
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from werkzeug.security import generate_password_hash
from werkzeug.serving import make_server

from app import ROOT, connect_db, create_app, digest
from conftest import BASE, Visitor


ACTOR = 'counter-demo'
NAME = 'Synthetic Admin Test Visitor'
PHONE = '010-0000-0074'
PREFIX = '/api/admin/'


@pytest.fixture(scope='module')
def credentials():
    password = secrets.token_urlsafe(24)
    return password, generate_password_hash(password, method='pbkdf2:sha256:1000000')


def provision(database, password_hash):
    path = Path(str(database) + '.admin.key')
    path.write_text(json.dumps({'actor': ACTOR, 'password_hash': password_hash}), encoding='utf-8')
    path.chmod(0o600)


@pytest.fixture
def site(tmp_path, credentials):
    database = tmp_path / 'staff.sqlite'
    provision(database, credentials[1])
    return create_app(database=database, public_url=BASE, dev=True, testing=True)


def post(client, action, data, csrf='', base=BASE):
    return client.post(PREFIX + action, json=data, base_url=base, headers={
        'Origin': base, 'X-Stamp-Client': '1', 'X-Admin-CSRF': csrf})


def get(client, action, base=BASE):
    return client.get(PREFIX + action, base_url=base)


def login(client, password, base=BASE):
    response = post(client, 'login', {'password': password}, base=base)
    assert response.status_code == 200
    assert response.json['actor'] == ACTOR
    return response.json['csrf']


@pytest.fixture
def staff(site, credentials):
    client = site.test_client()
    return client, login(client, credentials[0])


def register_card(app):
    visitor = Visitor(app, start=False)
    assert visitor.post('/api/register', {'name': NAME, 'phone': PHONE, 'notice_acknowledged': True}).status_code == 200
    visitor.read()
    with closing(connect_db(app.config['DATABASE'])) as db:
        card_id = db.execute('SELECT customer_id FROM sessions WHERE token_hash=?', (digest(visitor.cookie()),)).fetchone()[0]
    return visitor, card_id


def products(app):
    with closing(connect_db(app.config['DATABASE'])) as db:
        return dict(db.execute('SELECT id, qr_key FROM products ORDER BY id'))


def search(staff, data):
    client, csrf = staff
    response = post(client, 'search', data, csrf)
    assert response.status_code == 200
    return response.json


def cancellation(staff, card_id, product_id):
    details = search(staff, {'customer_id': card_id})
    stamp = next(s for s in details['stamps'] if s['product_id'] == product_id)
    assert stamp['collected_at'] is not None
    return {'customer_id': card_id, 'product_id': product_id, 'revision': stamp['revision']}


def cancel(staff, target):
    client, csrf = staff
    return post(client, 'cancel', target, csrf)


def test_customer_and_anonymous_sessions_cannot_reach_admin_data(site):
    visitor, card_id = register_card(site)
    visitor.collect(next(iter(products(site).values())))
    for client in (site.test_client(), visitor.client):
        assert get(client, 'session').status_code == 401
        responses = [get(client, 'stats'), post(client, 'search', {'phone': PHONE}, visitor.csrf),
                     post(client, 'cancel', {'customer_id': card_id, 'product_id': 'season-01', 'revision': 0}, visitor.csrf)]
        for response in responses:
            assert response.status_code == 401
            assert set(response.json) == {'error', 'code'}
            assert all(value not in response.text for value in (NAME, PHONE, card_id))
    assert visitor.read()['customer']['count'] == 1
    assert 'Set-Cookie' not in site.test_client().get('/admin', base_url=BASE).headers


def test_no_admin_credentials_disables_login_without_disrupting_customers(app, credentials):
    assert post(app.test_client(), 'login', {'password': credentials[0]}).status_code in (401, 403, 503)
    assert get(app.test_client(), 'stats').status_code != 200
    assert Visitor(app).read()['customer']['count'] == 0


def test_login_checks_origin_and_json_and_limits_even_valid_password_after_failures(site, credentials):
    client = site.test_client()
    assert client.post(PREFIX + 'login', json={'password': credentials[0]}, base_url=BASE).status_code == 403
    assert client.post(PREFIX + 'login', json={'password': credentials[0]}, base_url=BASE,
                       headers={'X-Stamp-Client': '1', 'Origin': 'https://other.invalid'}).status_code == 403
    assert client.post(PREFIX + 'login', data={'password': credentials[0]}, base_url=BASE,
                       headers={'X-Stamp-Client': '1', 'Origin': BASE}).status_code == 415
    for _ in range(5):
        rejected = post(site.test_client(), 'login', {'password': 'incorrect fixture'})
        assert rejected.status_code == 401 and 'Set-Cookie' not in rejected.headers
    limited = post(client, 'login', {'password': credentials[0]})
    assert limited.status_code == 429 and limited.headers.get('Retry-After')
    assert get(client, 'session').status_code == 401


def test_customer_admin_tokens_and_csrf_are_distinct_even_in_same_browser(site, credentials):
    visitor, card_id = register_card(site)
    client = visitor.client
    csrf = login(client, credentials[0])
    assert csrf != visitor.csrf
    for wrong_csrf in ('', visitor.csrf):
        assert post(client, 'search', {'customer_id': card_id}, wrong_csrf).status_code == 403
        assert post(client, 'logout', {}, wrong_csrf).status_code == 403
    product = next(iter(products(site).values()))
    assert client.post('/api/collect', json={'qr': product}, base_url=BASE, headers={
        'Origin': BASE, 'X-Stamp-Client': '1', 'X-CSRF-Token': csrf}).status_code == 403
    admin_token = client.get_cookie('stamp_admin_dev', domain=visitor.domain).value
    assert admin_token != visitor.cookie()
    fresh = Visitor(site, start=False)
    fresh.client.set_cookie(site.cookie_name, admin_token, domain=visitor.domain)
    assert fresh.read()['customer'] is None
    fresh.client.set_cookie('stamp_admin_dev', visitor.cookie(), domain=visitor.domain)
    assert get(fresh.client, 'stats').status_code == 401
    assert post(client, 'logout', {}, csrf).status_code == 200
    assert get(client, 'stats').status_code == 401
    fresh.client.set_cookie('stamp_admin_dev', admin_token, domain=visitor.domain)
    assert get(fresh.client, 'stats').status_code == 401
    assert visitor.collect(product)['added']


def test_secure_cookie_expiry_and_server_side_revocation(tmp_path, credentials, monkeypatch):
    database = tmp_path / 'https.sqlite'
    provision(database, credentials[1])
    event = json.loads((ROOT / 'event.json').read_text(encoding='utf-8'))
    event.update(draft=False, starts_at='2000-01-01T00:00:00+00:00', ends_at='2000-01-31T00:00:00+00:00',
                 reward_until='2000-02-01T00:00:00+00:00')
    config = tmp_path / 'event.json'
    config.write_text(json.dumps(event), encoding='utf-8')
    base = 'https://staff.example.invalid'
    app = create_app(config, database, base, testing=True)
    client = app.test_client()
    csrf = login(client, credentials[0], base)
    session = get(client, 'session', base)
    assert session.json['csrf'] == csrf
    expiry = session.json['expires_at']
    cookie = client.get_cookie('__Host-stamp-admin', domain='staff.example.invalid')
    assert cookie.secure and cookie.http_only and cookie.same_site in ('Strict', 'Lax')
    assert cookie.path == '/' and cookie.origin_only and 0 < cookie.max_age <= 8 * 3600
    assert session.headers['Cache-Control'] == 'no-store' and session.headers['Referrer-Policy'] == 'no-referrer'
    with closing(connect_db(database)) as db:
        assert cookie.value not in '\n'.join(db.iterdump())
    monkeypatch.setattr('app.time.time', lambda: expiry)
    assert get(client, 'stats', base).status_code == 401


def test_password_rotation_and_restart_require_fresh_admin_login(site, credentials):
    from staff_admin import write_staff_password

    database = site.config['DATABASE']
    client = site.test_client()
    login(client, credentials[0])
    updated = secrets.token_urlsafe(24)
    write_staff_password(database, ACTOR, updated)
    assert get(client, 'stats').status_code == 401
    assert post(client, 'login', {'password': credentials[0]}).status_code == 401
    login(client, updated)
    old_token = client.get_cookie('stamp_admin_dev', domain='127.0.0.1').value
    restarted = create_app(database=database, public_url=BASE, dev=True, testing=True)
    restored = restarted.test_client()
    restored.set_cookie('stamp_admin_dev', old_token, domain='127.0.0.1')
    assert get(restored, 'stats').status_code == 401
    login(restored, updated)
    assert get(restored, 'stats').status_code == 200
    with closing(connect_db(database)) as db:
        dump = '\n'.join(db.iterdump())
        assert updated not in dump and credentials[0] not in dump and credentials[1] not in dump


def test_search_by_id_and_normalized_phone_and_distribution_include_legacy_cards(site, staff):
    visitor, card_id = register_card(site)
    item_keys = products(site)
    for key in list(item_keys.values())[:2]:
        visitor.collect(key)
    completed = Visitor(site)
    for key in item_keys.values():
        completed.collect(key)
    assert completed.post('/api/redeem', {'confirm_received': True}).status_code == 200
    with closing(connect_db(site.config['DATABASE'])) as db:
        db.execute("INSERT INTO customers VALUES ('legacy-card', 1, NULL)")
        lookup = db.execute('SELECT phone_lookup_hmac FROM customer_profiles WHERE customer_id=?', (card_id,)).fetchone()[0]
    stats = get(staff[0], 'stats')
    assert stats.status_code == 200
    assert stats.json == {'cards': 3, 'stamps': 12, 'redeemed': 1, 'distribution': [1, 0, 1, 0, 0, 0, 0, 0, 0, 0, 1]}
    details = search(staff, {'phone': '+82 10 0000 0074'})
    assert details == search(staff, {'customer_id': card_id})
    assert set(details) == {'customer_id', 'name', 'redeemed_at', 'stamps'}
    assert details['name'] == NAME and details['redeemed_at'] is None
    assert {s['product_id'] for s in details['stamps']} == set(item_keys)
    held = visitor.read()['customer']['stamps']
    for stamp in details['stamps']:
        assert set(stamp) == {'product_id', 'name', 'collected_at', 'revision'}
        assert stamp['collected_at'] == held.get(stamp['product_id'])
    assert all(value not in json.dumps(details) for value in (PHONE, PHONE.replace('-', ''), lookup, *item_keys.values()))
    assert search(staff, {'customer_id': 'legacy-card'})['name'] is None
    assert post(staff[0], 'search', {'customer_id': 'unknown-fixture'}, staff[1]).status_code == 404
    for invalid in ({}, {'phone': '1234'}, {'phone': PHONE, 'customer_id': card_id}):
        assert post(staff[0], 'search', invalid, staff[1]).status_code == 400


def test_cancel_only_selected_stamp_and_audit_retries_across_same_second_recollection(site, staff, monkeypatch):
    # A timestamp alone cannot distinguish a re-collected stamp within the same second.
    clock = int(time.time())
    monkeypatch.setattr('app.time.time', lambda: clock)
    owner, card_id = register_card(site)
    neighbor = Visitor(site)
    first, second = list(products(site).items())[:2]
    for key in (first[1], second[1]):
        owner.collect(key)
    neighbor.collect(first[1])
    target = cancellation(staff, card_id, first[0])
    assert post(staff[0], 'cancel', target, owner.csrf).status_code == 403
    assert cancel(staff, target).status_code == 200
    assert owner.read()['customer']['stamps'] == {second[0]: clock}
    assert neighbor.read()['customer']['stamps'] == {first[0]: clock}
    totals = get(staff[0], 'stats').json
    assert totals['stamps'] == 2 and totals['distribution'][1] == 2
    assert cancel(staff, target).status_code == 409
    assert owner.collect(first[1])['added']
    assert cancel(staff, target).status_code == 409
    assert owner.read()['customer']['count'] == 2
    with closing(connect_db(site.config['DATABASE'])) as db:
        rows = list(db.execute('SELECT customer_id,product_id,original_collected_at,cancelled_at,admin_actor FROM stamp_cancel_audit'))
    assert [tuple(row) for row in rows] == [(card_id, first[0], clock, clock, ACTOR)]
    fresh = cancellation(staff, card_id, first[0])
    assert fresh['revision'] != target['revision']
    assert cancel(staff, fresh).status_code == 200


def test_audit_write_failure_rolls_back_deletion_before_retry(site, staff):
    owner, card_id = register_card(site)
    item, key = next(iter(products(site).items()))
    owner.collect(key)
    target = cancellation(staff, card_id, item)
    with closing(connect_db(site.config['DATABASE'])) as db:
        db.execute('''CREATE TRIGGER fail_audit BEFORE INSERT ON stamp_cancel_audit BEGIN
                      INSERT INTO missing_fixture_table VALUES (1); END''')
    assert cancel(staff, target).status_code == 503
    assert owner.read()['customer']['count'] == 1
    with closing(connect_db(site.config['DATABASE'])) as db:
        assert db.execute('SELECT COUNT(*) FROM stamp_cancel_audit').fetchone()[0] == 0
        db.execute('DROP TRIGGER fail_audit')
    assert cancel(staff, target).status_code == 200
    assert owner.read()['customer']['count'] == 0


def test_reward_completion_prevents_api_and_sql_cancellation(site, staff):
    owner, card_id = register_card(site)
    item_keys = products(site)
    for key in item_keys.values():
        owner.collect(key)
    target = cancellation(staff, card_id, next(iter(item_keys)))
    assert owner.post('/api/redeem', {'confirm_received': True}).status_code == 200
    assert cancel(staff, target).status_code == 409
    assert search(staff, {'customer_id': card_id})['redeemed_at'] == owner.read()['customer']['redeemed_at']
    with closing(connect_db(site.config['DATABASE'])) as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute('DELETE FROM stamps WHERE customer_id=? AND product_id=?', (card_id, target['product_id']))
        assert db.execute('SELECT COUNT(*) FROM stamp_cancel_audit').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM stamps').fetchone()[0] == 10
        assert db.execute('SELECT COUNT(*) FROM reward_audit').fetchone()[0] == 1


def parallel_calls(first, second):
    barrier = threading.Barrier(2)

    def start(call):
        barrier.wait(timeout=10)
        return call().status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(start, call) for call in (first, second)]
        return [f.result(timeout=20) for f in futures]


@pytest.mark.parametrize('opponent', ['cancel', 'redeem'])
def test_cancellation_races_in_separate_app_instances(site, credentials, opponent):
    # Different app.write_lock objects; the SQLite transaction decides the winner.
    second = create_app(database=site.config['DATABASE'], public_url=BASE, dev=True, testing=True)
    clients = [app.test_client() for app in (site, second)]
    first_staff, other_staff = [(client, login(client, credentials[0])) for client in clients]
    owner, card_id = register_card(second)
    item_keys = products(site)
    for key in item_keys.values():
        owner.collect(key)
    target = cancellation(first_staff, card_id, next(iter(item_keys)))
    if opponent == 'redeem':
        other_call = lambda: owner.post('/api/redeem', {'confirm_received': True})
    else:
        other_call = lambda: cancel(other_staff, target)
    first_status, other_status = parallel_calls(lambda: cancel(first_staff, target), other_call)
    assert sorted([first_status, other_status]) == [200, 409]
    redeemed = opponent == 'redeem' and other_status == 200
    with closing(connect_db(site.config['DATABASE'])) as db:
        assert db.execute('SELECT COUNT(*) FROM stamps').fetchone()[0] == (10 if redeemed else 9)
        assert db.execute('SELECT COUNT(*) FROM stamp_cancel_audit').fetchone()[0] == (0 if redeemed else 1)
        assert db.execute('SELECT COUNT(*) FROM reward_audit').fetchone()[0] == (1 if redeemed else 0)
        assert (db.execute('SELECT redeemed_at FROM customers WHERE id=?', (card_id,)).fetchone()[0] is not None) == redeemed


@pytest.mark.skipif(os.environ.get('MAISON_HTTP_TESTS') != '1', reason='Explicit loopback HTTP test opt-in')
def test_http_only_authorized_detail_contains_name_and_access_logs_omit_personal_data(site, credentials, caplog):
    _, card_id = register_card(site)
    password, password_hash = credentials
    caplog.set_level(logging.INFO)
    server = make_server('127.0.0.1', 0, site)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    headers = {'Host': urlsplit(BASE).netloc, 'Origin': BASE, 'X-Stamp-Client': '1', 'Content-Type': 'application/json'}

    def request(path, payload=None):
        with closing(HTTPConnection('127.0.0.1', server.server_port, timeout=5)) as conn:
            conn.request('POST' if payload is not None else 'GET', path,
                         json.dumps(payload) if payload is not None else None, headers)
            response = conn.getresponse()
            assert response.getheader('Location') is None and response.getheader('Cache-Control') == 'no-store'
            assert response.getheader('Referrer-Policy') == 'no-referrer'
            body = json.load(response)
            exposure = str(response.getheaders()) + str(body)
            for secret in (PHONE, PHONE.replace('-', ''), password, password_hash):
                assert secret not in exposure
            return response.status, body, response.getheader('Set-Cookie')

    try:
        status, denied, _ = request(PREFIX + 'search', {'phone': PHONE})
        assert status == 401 and NAME not in str(denied) and card_id not in str(denied)
        status, auth, cookie = request(PREFIX + 'login', {'password': password})
        assert status == 200 and NAME not in cookie and NAME not in str(auth)
        headers.update({'Cookie': cookie.split(';', 1)[0], 'X-Admin-CSRF': auth['csrf']})
        status, detail, _ = request(PREFIX + 'search', {'phone': PHONE})
        assert status == 200 and detail['name'] == NAME
        status, totals, _ = request(PREFIX + 'stats')
        assert status == 200 and NAME not in str(totals) and card_id not in str(totals)
        assert request(PREFIX + 'logout', {})[0] == 200
        assert request(PREFIX + 'stats')[0] == 401
    finally:
        server.shutdown()
        thread.join(5)
        server.server_close()
    for action in ('login', 'search', 'logout', 'stats'):
        assert PREFIX + action + ' HTTP' in caplog.text
    for secret in (NAME, PHONE, PHONE.replace('-', ''), card_id, password, password_hash,
                   auth['csrf'], headers['Cookie'].split('=', 1)[1]):
        assert secret not in caplog.text
