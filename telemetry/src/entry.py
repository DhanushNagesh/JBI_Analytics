import base64
import hmac
import json
import time
from urllib.parse import parse_qsl, urlparse

import js
from pyodide.ffi import to_js
from workers import Response, WorkerEntrypoint

import api
import cron
import ingest


def _json(data, status=200):
    return Response(json.dumps(data, separators=(",", ":")), status=status,
                    headers={"content-type": "application/json", "cache-control": "no-store"})


def _text(msg, status):
    return Response(msg, status=status, headers={"content-type": "text/plain"})


class D1:
    def __init__(self, db):
        self.db = db

    async def all(self, sql, params=()):
        res = await self.db.prepare(sql).bind(*params).all()
        return [dict(r) for r in res.results]

    async def batch(self, stmts):
        # D1 runs a batch as one transaction: a batch is stored whole or not at all.
        if stmts:
            await self.db.batch([self.db.prepare(s).bind(*p) for s, p in stmts])


def _b64url(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


_certs = {"at": 0, "keys": {}}


async def _access_key(team: str, kid: str):
    if kid not in _certs["keys"] or time.time() - _certs["at"] > 3600:
        res = await js.fetch(f"{team}/cdn-cgi/access/certs")
        body = (await res.json()).to_py()
        keys = {}
        for jwk in body.get("keys", []):
            keys[jwk["kid"]] = await js.crypto.subtle.importKey(
                "jwk", to_js(jwk, dict_converter=js.Object.fromEntries),
                to_js({"name": "RSASSA-PKCS1-v1_5", "hash": "SHA-256"}, dict_converter=js.Object.fromEntries),
                False, to_js(["verify"]),
            )
        _certs.update(at=time.time(), keys=keys)
    return _certs["keys"].get(kid)


async def access_ok(request, env) -> bool:
    """Verifies the Cloudflare Access JWT, so /api/* stays closed even on a hostname Access
    does not cover (the workers.dev URL, if Access is only on a custom domain)."""
    if str(env.DEV_NO_AUTH or "") == "1":
        return True
    team, aud = str(env.ACCESS_TEAM_DOMAIN or "").rstrip("/"), str(env.ACCESS_AUD or "")
    token = request.headers.get("cf-access-jwt-assertion")
    if not team or not aud or not token:
        return False
    try:
        h64, p64, s64 = token.split(".")
        header, claims = json.loads(_b64url(h64)), json.loads(_b64url(p64))
        auds = claims.get("aud") if isinstance(claims.get("aud"), list) else [claims.get("aud")]
        if header.get("alg") != "RS256" or aud not in auds or claims.get("iss") != team:
            return False
        if claims.get("exp", 0) < time.time():
            return False
        key = await _access_key(team, header.get("kid"))
        if key is None:
            return False
        return bool(await js.crypto.subtle.verify(
            "RSASSA-PKCS1-v1_5", key, to_js(_b64url(s64)), to_js(f"{h64}.{p64}".encode()),
        ))
    except Exception:
        return False


# Public and cached for a minute per isolate. Shields.io polls these for the README badges.
BADGE_TTL = 60
_badges = {}

API = {"/api/live": api.live, "/api/summary": api.summary, "/api/ccu": api.ccu,
       "/api/top": api.top, "/api/rounds": api.rounds}


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        url = urlparse(request.url)
        path, method = url.path, request.method
        now = int(time.time())
        db = D1(self.env.DB)

        if path == "/ingest":
            if method != "POST":
                return _text("method not allowed", 405)
            return await self.ingest(request, db, now)

        if path.startswith("/badge/") and method == "GET":
            return await self.badge(path.rsplit("/", 1)[1], db, now)

        if not await access_ok(request, self.env):
            return _text("forbidden", 403)

        q = dict(parse_qsl(url.query))
        if method == "GET" and path in API:
            try:
                return _json(await API[path](db, q, now))
            except ValueError as e:
                return _text(str(e), 400)
        if method == "DELETE" and path.startswith("/api/user/"):
            uid = path.rsplit("/", 1)[1]
            if not uid.isdigit():
                return _text("bad user id", 400)
            await db.batch(api.delete_user_statements(int(uid)))
            return Response("", status=204)
        return _text("not found", 404)

    async def ingest(self, request, db, now):
        expected = str(self.env.INGEST_TOKEN or "")
        given = request.headers.get("authorization") or ""
        if not expected or not hmac.compare_digest(given.encode(), f"Bearer {expected}".encode()):
            return _text("unauthorized", 401)
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > ingest.MAX_BODY:
            return _text("body over 256 KB", 400)
        try:
            events = ingest.parse_batch(await request.bytes())
        except ingest.BadRequest as e:
            return _text(str(e), 400)
        drop = {s.strip() for s in str(self.env.DROP_INFO or "").split(",") if s.strip()}
        stmts, skipped = ingest.statements_for(events, now, drop)
        await db.batch(stmts)
        if skipped:
            print(f"ingest: skipped {skipped} malformed events")
        return Response("", status=204)

    async def badge(self, metric, db, now):
        hit = _badges.get(metric)
        if hit is None or now - hit[0] >= BADGE_TTL:
            try:
                hit = (now, await api.badge(db, metric, now))
            except KeyError:
                return _text("not found", 404)
            _badges[metric] = hit
        return Response(json.dumps(hit[1]), headers={
            "content-type": "application/json", "cache-control": f"public, max-age={BADGE_TTL}"})

    async def scheduled(self, controller, env=None, ctx=None):
        await D1(self.env.DB).batch(cron.statements_for(int(time.time())))
