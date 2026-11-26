"""Staff-only card management, with synthetic credentials and isolated databases."""
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextlib import closing
from datetime import datetime
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from werkzeug.security import generate_password_hash
from werkzeug.serving import make_server

from app import ROOT, connect_db, create_app, digest
from conftest import BASE, Visitor, fixture_app


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
    return fixture_app(database=database, public_url=BASE, dev=True, testing=True)


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
    assert visitor.post('/api/register', {'name': NAME, 'phone': PHONE, 'notice_acknowledged': True, 'notice_version': 'synthetic-contact-v1'}).status_code == 200
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
    app = fixture_app(config, database, base, testing=True)
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
    customer = Visitor(site)
    original_cookie = customer.cookie()
    updated = secrets.token_urlsafe(24)
    write_staff_password(database, ACTOR, updated)
    assert customer.read()['customer']['registered'] and customer.cookie() == original_cookie
    assert get(client, 'stats').status_code == 401
    assert post(client, 'login', {'password': credentials[0]}).status_code == 401
    login(client, updated)
    old_token = client.get_cookie('stamp_admin_dev', domain='127.0.0.1').value
    restarted = fixture_app(database=database, public_url=BASE, dev=True, testing=True)
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
    assert set(details) == {'customer_id', 'name', 'phone_masked', 'redeemed_at', 'stamps',
                            'permissions', 'correction_revision'}
    assert not details['permissions']['can_correct']
    assert details['name'] == NAME and details['redeemed_at'] is None
    assert {s['product_id'] for s in details['stamps']} == set(item_keys)
    assert details['phone_masked'] == '010-****-' + PHONE[-4:]
    assert search(staff, {'customer_id': 'legacy-card'})['phone_masked'] is None
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


CUTOFF_KST = '2027-02-01T00:00:00+09:00'
CUTOFF_UTC = '2027-01-31T15:00:00+00:00'
CUTOFF_EPOCH = int(datetime.fromisoformat(CUTOFF_UTC).timestamp())


@pytest.fixture
def complete_card_before_cutoff(site, credentials, monkeypatch):
    site.event.update(draft=False, starts_at='2026-12-01T00:00:00+09:00',
                      ends_at=CUTOFF_KST, reward_until='2027-03-01T00:00:00+09:00')
    monkeypatch.setattr(time, 'time', lambda: CUTOFF_EPOCH - 1)
    client = site.test_client()
    staff = client, login(client, credentials[0])
    owner, card_id = register_card(site)
    items = products(site)
    for key in items.values():
        assert owner.collect(key)['added']
    target = cancellation(staff, card_id, next(iter(items)))
    return owner, staff, target


@pytest.mark.parametrize('ends_at', [CUTOFF_KST, CUTOFF_UTC], ids=['KST', 'UTC'])
@pytest.mark.parametrize('offset', [-1, 0, 1], ids=['before', 'at', 'after'])
def test_staff_cancel_cutoff(site, complete_card_before_cutoff, monkeypatch, ends_at, offset):
    owner, staff, target = complete_card_before_cutoff
    site.event['ends_at'] = ends_at
    before = owner.read()['customer']['stamps']
    assert len(before) == 10
    now = CUTOFF_EPOCH + offset
    monkeypatch.setattr(time, 'time', lambda: now)
    response = cancel(staff, target)

    expected = before.copy()
    audit = []
    if offset < 0:
        assert response.status_code == 200
        pid = target['product_id']
        audit.append((target['customer_id'], pid, expected.pop(pid), now, ACTOR))
    else:
        assert response.status_code == 409 and response.json['code'] == 'cancel_closed'
    with closing(connect_db(site.config['DATABASE'])) as db:
        assert dict(db.execute('SELECT product_id, collected_at FROM stamps')) == expected
        rows = db.execute('SELECT customer_id, product_id, original_collected_at, cancelled_at, admin_actor '
                          'FROM stamp_cancel_audit')
        assert [tuple(row) for row in rows] == audit
    if offset >= 0:
        assert owner.post('/api/redeem', {'confirm_received': True}).status_code == 200


def test_staff_cancel_waiting_for_sqlite_writer_at_cutoff(site, complete_card_before_cutoff, monkeypatch):
    owner, staff, target = complete_card_before_cutoff
    before = owner.read()['customer']['stamps']
    begin = threading.Event()

    def observe_begin(sql):
        if sql == 'BEGIN IMMEDIATE':
            begin.set()

    def observed_connect(*args, **kwargs):
        db = connect_db(*args, **kwargs)
        db.set_trace_callback(observe_begin)
        return db

    with closing(connect_db(site.config['DATABASE'])) as holder, ThreadPoolExecutor(max_workers=1) as pool:
        holder.execute('BEGIN IMMEDIATE')
        monkeypatch.setattr('app.connect_db', observed_connect)
        pending = pool.submit(cancel, staff, target)
        try:
            assert begin.wait(timeout=5)
            # SQLite is executing the request's BEGIN while another connection owns the writer.
            with pytest.raises(FutureTimeout):
                pending.result(timeout=0.05)
            monkeypatch.setattr(time, 'time', lambda: CUTOFF_EPOCH)
        finally:
            holder.rollback()
        response = pending.result(timeout=5)
        assert response.status_code == 409 and response.json['code'] == 'cancel_closed'
        assert dict(holder.execute('SELECT product_id, collected_at FROM stamps')) == before
        assert holder.execute('SELECT COUNT(*) FROM stamp_cancel_audit').fetchone()[0] == 0
        assert holder.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not holder.execute('PRAGMA foreign_key_check').fetchall()
    assert owner.post('/api/redeem', {'confirm_received': True}).status_code == 200


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
    second = fixture_app(database=site.config['DATABASE'], public_url=BASE, dev=True, testing=True)
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


REWARD_END = int(datetime.fromisoformat('2027-03-01T00:00:00+09:00').timestamp())
MANAGER = 'manager-fixture'
CORRECTION_REASON = '진열 상품표와 적립 상품의 불일치를 확인했습니다.'


@pytest.fixture(scope='module')
def manager_credentials():
    return secrets.token_urlsafe(24)


def manager_session(site, password):
    client = site.test_client()
    response = post(client, 'manager-login', {'password': password})
    assert response.status_code == 200
    assert response.json['role'] == 'admin' and response.json['actor'] == MANAGER
    return client, response.json['csrf']


@pytest.fixture
def corrections(site, manager_credentials, monkeypatch):
    from correction_schema import apply_correction_schema

    database = site.config['DATABASE']
    with closing(connect_db(database)) as db:
        db.execute('BEGIN IMMEDIATE')
        apply_correction_schema(db)
        db.commit()
    from staff_admin import write_staff_password
    write_staff_password(database, MANAGER, manager_credentials, role='admin')
    site.event.update(draft=False, starts_at='2026-12-01T00:00:00+09:00', ends_at=CUTOFF_KST,
                      reward_until='2027-03-01T00:00:00+09:00')
    monkeypatch.setattr(time, 'time', lambda: CUTOFF_EPOCH - 1)
    owner, card = register_card(site)
    items = products(site)
    for key in list(items.values())[:2]:
        assert owner.collect(key)['added']
    manager = manager_session(site, manager_credentials)
    monkeypatch.setattr(time, 'time', lambda: CUTOFF_EPOCH)
    return manager, card, list(items), owner


def correction_request(manager, card, product, replacement=None):
    return {'customer_id': card, 'product_id': product,
            'revision': search(manager, {'customer_id': card})['correction_revision'],
            'request_id': secrets.token_hex(16), 'type': 'replace' if replacement else 'remove',
            'new_product_id': replacement, 'reason_code': 'wrong_product' if replacement else 'erroneous_stamp',
            'reason_detail': CORRECTION_REASON}


def correct(manager, request):
    client, csrf = manager
    return post(client, 'correct', request, csrf)


def correction_state(site, card):
    with closing(connect_db(site.config['DATABASE'])) as db:
        stamps = dict(db.execute('SELECT product_id,collected_at FROM stamps WHERE customer_id=?', (card,)))
        audits = [dict(row) for row in db.execute(
            'SELECT * FROM stamp_correction_audit WHERE customer_id=? ORDER BY id', (card,))]
        rewards = [tuple(row) for row in db.execute('SELECT * FROM reward_audit WHERE customer_id=?', (card,))]
        return stamps, audits, rewards


def test_correction_authority_comes_from_separate_credentials(site, corrections, credentials, manager_credentials):
    manager, card, items, owner = corrections
    client = site.test_client()
    staff = client, login(client, credentials[0])
    data = correction_request(manager, card, items[0])
    before = correction_state(site, card)
    assert get(client, 'session').json['role'] == 'staff'
    assert get(manager[0], 'session').json['role'] == 'admin'
    assert not search(staff, {'customer_id': card})['permissions']['can_correct']
    assert search(manager, {'customer_id': card})['permissions']['can_correct']
    assert correct(staff, data).status_code == 403
    assert correct(staff, dict(data, role='admin')).status_code == 403
    assert post(site.test_client(), 'correct', data).status_code == 401
    assert post(owner.client, 'correct', data, owner.csrf).status_code == 401
    assert post(site.test_client(), 'manager-login', {'password': credentials[0]}).status_code == 401
    assert post(site.test_client(), 'login', {'password': manager_credentials}).status_code == 401
    assert correct(manager, dict(data, role='admin')).status_code == 400
    for csrf in ('', staff[1], owner.csrf):
        assert post(manager[0], 'correct', data, csrf).status_code == 403
    assert correction_state(site, card) == before


def test_expired_manager_cannot_correct(site, corrections, monkeypatch):
    manager, card, items, _ = corrections
    data = correction_request(manager, card, items[0])
    expires_at = get(manager[0], 'session').json['expires_at']
    before = correction_state(site, card)
    monkeypatch.setattr(time, 'time', lambda: expires_at)
    assert correct(manager, data).status_code == 401
    assert correction_state(site, card) == before


@pytest.mark.parametrize('offset,allowed', [(-1, False), (0, True), (1, True)])
def test_manager_correction_start(corrections, site, monkeypatch, offset, allowed):
    manager, card, items, _ = corrections
    site.event['ends_at'] = CUTOFF_UTC
    monkeypatch.setattr(time, 'time', lambda: CUTOFF_EPOCH + offset)
    details = search(manager, {'customer_id': card})
    assert details['permissions']['can_correct'] is allowed
    before = correction_state(site, card)
    response = correct(manager, correction_request(manager, card, items[0]))
    if allowed:
        assert response.status_code == 200
    else:
        assert response.status_code == 409 and response.json['code'] == 'correction_closed'
        assert correction_state(site, card) == before


@pytest.mark.parametrize('reward_until', ['2027-03-01T00:00:00+09:00', '2027-02-28T15:00:00+00:00'])
@pytest.mark.parametrize('offset', [-1, 0, 1])
def test_manager_correction_end(site, corrections, manager_credentials, monkeypatch, reward_until, offset):
    _, card, items, _ = corrections
    site.event['reward_until'] = reward_until
    monkeypatch.setattr(time, 'time', lambda: REWARD_END + offset)
    manager = manager_session(site, manager_credentials)
    permission = search(manager, {'customer_id': card})['permissions']
    assert permission['can_correct'] is (offset < 0)
    before = correction_state(site, card)
    response = correct(manager, correction_request(manager, card, items[0]))
    if offset < 0:
        assert response.status_code == 200
    else:
        assert response.status_code == 409 and response.json['code'] == 'correction_closed'
        assert correction_state(site, card) == before


@pytest.mark.parametrize('replace', [True, False], ids=['replace', 'remove'])
def test_correction_changes_only_held_stamp_and_appends_exact_audit(site, corrections, replace):
    manager, card, items, owner = corrections
    original, target = items[0], items[2] if replace else None
    data = correction_request(manager, card, original, target)
    before, _, _ = correction_state(site, card)
    response = correct(manager, data)
    assert response.status_code == 200
    expected = before.copy()
    collected_at = expected.pop(original)
    if replace:
        expected[target] = collected_at
    stamps, audits, rewards = correction_state(site, card)
    assert stamps == expected == owner.read()['customer']['stamps']
    assert not rewards
    assert len(audits) == 1
    audit = audits[0]
    assert {key: value for key, value in audit.items() if key != 'id'} == {
        'request_id': data['request_id'], 'customer_id': card, 'admin_actor': MANAGER,
        'corrected_at': CUTOFF_EPOCH, 'correction_type': data['type'], 'reason_code': data['reason_code'],
        'reason_detail': CORRECTION_REASON, 'old_product_id': original, 'new_product_id': target,
        'original_collected_at': collected_at, 'stamp_count_after': len(expected),
        'eligible_before': 0, 'eligible_after': 0}
    with closing(connect_db(site.config['DATABASE'])) as db:
        assert db.execute('SELECT COUNT(*) FROM stamp_cancel_audit').fetchone()[0] == 0
    assert not search(manager, {'customer_id': card})['permissions']['can_cancel']


def test_invalid_correction_reason_or_operation_leaves_card_intact(site, corrections):
    manager, card, items, _ = corrections
    valid = correction_request(manager, card, items[0], items[2])
    before = correction_state(site, card)
    for detail in (None, '', ' ' * 20, '가' * 9, '가' * 301, '전화 연락 ' + PHONE,
                   '고객 연락 fake@example.invalid', NAME + ' 상품 확인'):
        payload = dict(valid, reason_detail=detail)
        if detail is None:
            del payload['reason_detail']
        response = correct(manager, payload)
        assert response.status_code == 400
        if detail and detail.strip():
            assert detail not in response.text
    for fields in ({'type': 'add'}, {'reason_code': 'other'}, {'type': 'remove'}, {'new_product_id': None},
                   {'new_product_id': items[0]}, {'new_product_id': items[1]}, {'new_product_id': 'absent'},
                   {'product_id': items[3]}, {'product_id': 'absent'}, {'request_id': 'invalid'},
                   {'revision': True}):
        assert correct(manager, dict(valid, **fields)).status_code in (400, 404, 409)
    assert correction_state(site, card) == before


def test_duplicate_request_and_stale_revision_do_not_change_second_stamp(site, corrections):
    manager, card, items, _ = corrections
    first = correction_request(manager, card, items[0], items[2])
    stale = correction_request(manager, card, items[1])
    assert correct(manager, first).status_code == 200
    before = correction_state(site, card)
    fresh = correction_request(manager, card, items[1])
    assert fresh['revision'] != stale['revision']
    assert correct(manager, dict(fresh, request_id=first['request_id'])).status_code == 409
    assert correct(manager, stale).status_code == 409
    assert correction_state(site, card) == before
    assert correct(manager, fresh).status_code == 200


@pytest.mark.parametrize('redeemed', [False, True], ids=['eligible', 'already-redeemed'])
def test_manager_preserves_complete_card_and_reward(site, corrections, monkeypatch, redeemed):
    manager, card, items, owner = corrections
    monkeypatch.setattr(time, 'time', lambda: CUTOFF_EPOCH - 1)
    for key in list(products(site).values())[2:]:
        assert owner.collect(key)['added']
    monkeypatch.setattr(time, 'time', lambda: CUTOFF_EPOCH)
    if redeemed:
        assert owner.post('/api/redeem', {'confirm_received': True}).status_code == 200
    before = correction_state(site, card)
    assert len(before[0]) == 10 and len(before[2]) == int(redeemed)
    assert not search(manager, {'customer_id': card})['permissions']['can_correct']
    for replacement in (None, items[1]):
        response = correct(manager, correction_request(manager, card, items[0], replacement))
        assert response.status_code == 409
    assert correction_state(site, card) == before
    if not redeemed:
        assert owner.post('/api/redeem', {'confirm_received': True}).status_code == 200
    assert len(correction_state(site, card)[2]) == 1


@pytest.mark.parametrize('failure', ["RAISE(ABORT, 'audit unavailable')", 'RAISE(IGNORE)'],
                         ids=['insert-error', 'silent-skip'])
def test_failed_correction_audit_rolls_back_and_same_request_can_retry(site, corrections, monkeypatch, failure):
    manager, card, items, _ = corrections
    payload = correction_request(manager, card, items[0], items[2])
    before = correction_state(site, card)
    observed = []

    def connect_with_fault(*args, **kwargs):
        db = connect_db(*args, **kwargs)
        # Only this request sees the fault. The persisted schema and signature stay intact.
        db.execute(f"""CREATE TEMP TRIGGER fail_fixture BEFORE INSERT ON main.stamp_correction_audit
                       BEGIN SELECT {failure}; END""")
        db.set_trace_callback(lambda sql: observed.append(sql.lstrip().split()[0].upper()))
        return db

    with monkeypatch.context() as scoped:
        scoped.setattr('app.connect_db', connect_with_fault)
        assert correct(manager, payload).status_code == 503
    assert 'UPDATE' in observed and 'ROLLBACK' in observed
    assert correction_state(site, card) == before
    with closing(connect_db(site.config['DATABASE'])) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
    assert correct(manager, payload).status_code == 200


@pytest.mark.parametrize('change', ['reward_end', 'logout'], ids=['period-ends', 'session-revoked'])
def test_correction_rechecks_after_waiting_for_writer(site, corrections, manager_credentials, monkeypatch, change):
    manager, card, items, _ = corrections
    monkeypatch.setattr(time, 'time', lambda: REWARD_END - 1)
    manager = manager_session(site, manager_credentials)
    payload = correction_request(manager, card, items[0])
    before = correction_state(site, card)
    begin = threading.Event()

    def connect_with_signal(*args, **kwargs):
        db = connect_db(*args, **kwargs)
        db.set_trace_callback(lambda sql: begin.set() if sql == 'BEGIN IMMEDIATE' else None)
        return db

    with closing(connect_db(site.config['DATABASE'])) as writer, ThreadPoolExecutor(max_workers=1) as pool:
        writer.execute('BEGIN IMMEDIATE')
        monkeypatch.setattr('app.connect_db', connect_with_signal)
        pending = pool.submit(correct, manager, payload)
        try:
            assert begin.wait(timeout=5)
            with pytest.raises(FutureTimeout):
                pending.result(timeout=0.05)
            if change == 'logout':
                writer.execute('DELETE FROM admin_sessions WHERE admin_actor=?', (MANAGER,))
            else:
                monkeypatch.setattr(time, 'time', lambda: REWARD_END)
        finally:
            writer.commit()
        response = pending.result(timeout=5)
    assert response.status_code == (401 if change == 'logout' else 409)
    assert correction_state(site, card) == before


def test_manager_can_search_but_cannot_correct_without_explicit_migration(site, manager_credentials, monkeypatch):
    from staff_admin import write_staff_password

    database = site.config['DATABASE']
    write_staff_password(database, MANAGER, manager_credentials, role='admin')
    site.event.update(draft=False, starts_at='2026-12-01T00:00:00+09:00', ends_at=CUTOFF_KST,
                      reward_until='2027-03-01T00:00:00+09:00')
    monkeypatch.setattr(time, 'time', lambda: CUTOFF_EPOCH - 1)
    owner, card = register_card(site)
    product, key = next(iter(products(site).items()))
    assert owner.collect(key)['added']
    monkeypatch.setattr(time, 'time', lambda: CUTOFF_EPOCH)
    manager = manager_session(site, manager_credentials)
    assert not search(manager, {'customer_id': card})['permissions']['can_correct']
    response = correct(manager, correction_request(manager, card, product))
    assert response.status_code == 503 and response.json['code'] == 'correction_unavailable'
    with closing(connect_db(database)) as db:
        assert dict(db.execute('SELECT product_id,collected_at FROM stamps')) == {product: CUTOFF_EPOCH - 1}
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='stamp_correction_audit'").fetchone()


def test_pre_close_cancel_and_recollection_invalidate_card_revision(site, corrections, credentials, monkeypatch):
    manager, card, items, owner = corrections
    monkeypatch.setattr(time, 'time', lambda: CUTOFF_EPOCH - 1)
    client = site.test_client()
    staff = client, login(client, credentials[0])
    stale = correction_request(manager, card, items[0])
    before = correction_state(site, card)
    assert cancel(staff, cancellation(staff, card, items[0])).status_code == 200
    assert owner.collect(products(site)[items[0]])['added']
    assert correction_state(site, card) == before  # even the collected timestamps match
    monkeypatch.setattr(time, 'time', lambda: CUTOFF_EPOCH)
    response = correct(manager, stale)
    assert response.status_code == 409 and response.json['code'] == 'stamp_changed'
    assert correction_state(site, card) == before


@pytest.mark.parametrize('now, expected_period, next_boundary', [
    (CUTOFF_EPOCH - 0.25, 'collection', CUTOFF_EPOCH),
    (CUTOFF_EPOCH, 'correction', REWARD_END),
    (REWARD_END - 0.25, 'correction', REWARD_END),
    (REWARD_END, 'closed', None),
])
def test_authenticated_session_clock_matches_card_permissions(site, credentials, monkeypatch, now,
                                                             expected_period, next_boundary):
    site.event.update(draft=False, starts_at='2026-12-01T00:00:00+09:00',
                      ends_at=CUTOFF_KST, reward_until='2027-03-01T00:00:00+09:00')
    monkeypatch.setattr(time, 'time', lambda: now)
    with closing(connect_db(site.config['DATABASE'])) as db:
        db.execute("INSERT INTO customers(id, created_at) VALUES ('clock-fixture', 1)")
    client = site.test_client()
    assert get(client, 'session').status_code == 401
    login_reply = post(client, 'login', {'password': credentials[0]})
    assert login_reply.status_code == 200
    for body in (login_reply.json, get(client, 'session').json):
        assert body['server_now'] == now and body['next_transition_at'] == next_boundary
        assert body['period'] == expected_period
    card = search((client, login_reply.json['csrf']), {'customer_id': 'clock-fixture'})
    assert card['permissions']['period'] == expected_period


def test_session_clock_has_no_scheduled_transition_for_draft(site, staff):
    assert site.event['draft']
    body = get(staff[0], 'session').json
    assert body['period'] == 'collection' and body['next_transition_at'] is None
    assert body['server_now'] < body['expires_at']
