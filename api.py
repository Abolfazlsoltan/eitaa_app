"""Async client for the Eitaa Channel Manager backend (same FastAPI server the web/desktop app uses)."""
import httpx

class ApiError(Exception): pass

class Api:
    def __init__(self):
        self.base = ""; self.token = None; self.on_401 = lambda: None
        self.c = httpx.AsyncClient(timeout=120)

    async def req(self, method, path, json=None, files=None):
        h = {"Authorization": "Bearer " + self.token} if self.token else {}
        try:
            r = await self.c.request(method, self.base.rstrip("/") + "/api" + path, json=json, files=files, headers=h)
        except (httpx.HTTPError, ValueError):
            raise ApiError("اتصال به سرور برقرار نشد. آدرس سرور و اینترنت را بررسی کنید.")
        if r.status_code == 401 and self.token: self.on_401()
        try: j = r.json()
        except ValueError: j = {}
        if r.status_code >= 400:
            d = j.get("detail") if isinstance(j, dict) else None
            raise ApiError(d if isinstance(d, str) else "خطای نامشخص رخ داد.")
        return j

    get = lambda self, p: self.req("GET", p)
    post = lambda self, p, body=None: self.req("POST", p, json=body if body is not None else {})
    delete = lambda self, p: self.req("DELETE", p)

    async def upload(self, name, data: bytes):
        return await self.req("POST", "/media", files={"file": (name, data)})
