"""Optional, separately licensed stamp illustrations. The PROD ZIP carries no artwork."""
import hashlib
import os
import stat
from pathlib import Path

from flask import Response, abort


# Digests of the ten original PNGs documented in docs/ASSET_ATTRIBUTION.md.
STAMP_DIGESTS = {
    "season-01": "db904e4c9801e386f045fd6b8b58309f7d435f88a3f1f061012748661589d3e4",
    "season-02": "d425b250f9e4ff8cc7b2853a61c886bfe0c4995700621f52472ae6158e28bda0",
    "season-03": "85c89f89b85c312a9247114e758c8b89fc8b6475b9c541bb835d29d080f7e6c6",
    "season-04": "88f0799081ac8d9c6690e341780b053f03e6cfefd9288cc8625844c715d1459f",
    "select-01": "141504c8aa3c4a9f30dd7dcc1d7aa707cb5f257add0185fbb60c494028de027c",
    "select-02": "6a33f00eaa6cac40db0d340dbec47f9a9a2d4da76c0861de807857f32762baff",
    "select-03": "6f79bcdde6a77ade940c77a8ae4a2862af6af61d59bd1d8df1f2d5133bd56684",
    "select-04": "b3d3f618d92e026ba6e0e2c0661d996271af7466373d02e246f8728c0307255e",
    "select-05": "79c9f149006e66048b13febeed08158f805e2730d0c795ebff8a36b821dc2b60",
    "select-06": "8fc96d1137c61cb9d22aba5bd3f4f2561c3592b27171cafd931a63ae23a14ac8",
}


def install_stamp_art(app, event):
    location = os.environ.get("MAISON_PROD_APPROVED_STAMP_DIR")
    if not location:
        return
    directory = Path(location)
    ids = {p["id"] for p in event["products"]}
    if (directory.name != event["id"] or directory.resolve(strict=True) != directory
            or directory.is_relative_to(Path(__file__).resolve().parents[2])
            or ids != STAMP_DIGESTS.keys()
            or {p.name for p in directory.iterdir()} != {f"{pid}.png" for pid in ids}):
        raise ValueError("Expected a separate event directory with exactly ten approved stamp illustrations")

    # Load the small, verified set once. No request path is used to access the filesystem.
    pictures = {}
    for pid, expected in STAMP_DIGESTS.items():
        file = directory / f"{pid}.png"
        with os.fdopen(os.open(file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 8192:
                raise ValueError("Expected a regular stamp image")
            data = stream.read(8193)
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError("Approved stamp image fingerprint mismatch")
        pictures[pid] = data

    @app.context_processor
    def stamp_art_enabled():
        return {"prod_stamp_art": True}

    @app.get("/stamp-illustrations/<product_id>.png")
    def stamp_illustration(product_id):
        if product_id not in pictures:
            abort(404)
        return Response(pictures[product_id], mimetype="image/png")
