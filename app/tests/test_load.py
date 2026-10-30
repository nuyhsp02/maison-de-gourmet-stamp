"""400 real SQLite collections, processed by 30 concurrent application clients."""
from concurrent.futures import ThreadPoolExecutor

from app import connect_db
from conftest import Visitor


def test_400_visitors_30_concurrent(app, keys):
    def visit(index):
        v = Visitor(app)
        order = keys[index % 10:] + keys[:index % 10]
        for key in order:
            assert v.collect(key)["added"] is True
        assert v.collect(order[0])["added"] is False
        assert v.read()["customer"]["status"] == "complete"
        result = v.post("/api/redeem", {"confirm_received": True})
        assert result.status_code == 200 and result.json["already_redeemed"] is False
        assert v.read()["customer"]["status"] == "redeemed"
        return True
    with ThreadPoolExecutor(max_workers=30) as pool:
        assert all(pool.map(visit, range(400)))
    with connect_db(app.config["DATABASE"]) as db:
        assert db.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 400
        assert db.execute("SELECT COUNT(*) FROM stamps").fetchone()[0] == 4000
        assert db.execute("SELECT COUNT(*) FROM reward_audit").fetchone()[0] == 400
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
