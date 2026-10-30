"""CI synthetic legacy-engine customer/QR fixture; no operational data paths."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--legacy', required=True)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    event = json.loads((repo / 'app/event.json').read_text())
    token, csrf, recovery = (secrets.token_urlsafe(32) for _ in range(3))
    products = {p['id']: secrets.token_urlsafe(24) for p in event['products']}
    signature = json.dumps([event['id'], sorted(products)])
    digest = lambda value: hashlib.sha256(value.encode()).hexdigest()
    with tempfile.TemporaryDirectory(prefix='maison-legacy-') as temp:
        database = Path(temp) / 'customer.sqlite'
        payload = {'database': str(database), 'schema': (repo/'app/schema.sql').read_text(),
                   'signature': signature, 'products': products, 'session': digest(token),
                   'csrf': csrf, 'recovery': digest(recovery)}
        script = '''import sqlite3,json,sys
from contextlib import closing
p=json.load(sys.stdin)
with closing(sqlite3.connect(p['database'])) as db:
 db.executescript(p['schema'])
 db.execute("INSERT INTO metadata VALUES ('event',?)",(p['signature'],))
 db.executemany('INSERT INTO products VALUES (?,?)',p['products'].items())
 db.execute("INSERT INTO customers VALUES ('fixture-customer',1,NULL)")
 db.execute("INSERT INTO sessions VALUES (?,'fixture-customer',?,4102444800)",(p['session'],p['csrf']))
 db.execute("INSERT INTO recovery VALUES ('fixture-customer',?,4102444800)",(p['recovery'],))
 for product in list(p['products'])[:3]: db.execute("INSERT INTO stamps VALUES ('fixture-customer',?,1)",(product,))
 db.commit()
 print(json.dumps({'legacy_sqlite':sqlite3.sqlite_version,'legacy_source_id':db.execute('SELECT sqlite_source_id()').fetchone()[0]}))
'''
        seeded = subprocess.run([args.legacy, '-c', script], input=json.dumps(payload),
                                text=True, stdout=subprocess.PIPE, check=True)
        legacy = json.loads(seeded.stdout)
        sys.path.insert(0, str(repo/'app'))
        from app import create_app
        app = create_app(database=database, dev=True, testing=True)
        base = 'http://127.0.0.1:8765'
        client = app.test_client()
        client.set_cookie(app.cookie_name, token, domain='127.0.0.1')
        def post(path, data, current_csrf):
            return client.post(path,json=data,base_url=base,headers={'Origin':base,'X-Stamp-Client':'1','X-CSRF-Token':current_csrf})
        assert client.get('/api/state',base_url=base).json['customer']['count']==3
        assert post('/api/recover', {'token':recovery},csrf).status_code==200
        current = client.get('/api/state',base_url=base).json
        assert current['customer']['count']==3
        for i,key in enumerate(products.values()):
            result=post('/api/collect',{'qr':key},current['csrf'])
            assert result.status_code==200 and result.json['added']==(i>=3)
        assert post('/api/redeem',{'confirm_received':True},current['csrf']).json['already_redeemed'] is False
        assert post('/api/redeem',{'confirm_received':True},current['csrf']).json['already_redeemed'] is True
        with closing(sqlite3.connect(database)) as db:
            assert dict(db.execute('SELECT id,qr_key FROM products'))==products
            assert db.execute('SELECT COUNT(*) FROM customers').fetchone()[0]==1
            assert db.execute('SELECT COUNT(*) FROM stamps').fetchone()[0]==10
            assert db.execute('SELECT COUNT(*) FROM reward_audit').fetchone()[0]==1
            assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        print('CROSS_RUNTIME315 '+json.dumps({**legacy,'target_sqlite':sqlite3.sqlite_version,
              'existing_cookie_recovery_stamps_and_qr_preserved':True,'reward_once':True}))


if __name__=='__main__': main()
