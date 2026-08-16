from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


USER_AGENT = "DealRadar-private/0.1 (RSS reader; private use)"


class HttpError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def get_bytes(
    url: str,
    timeout: int = 30,
    headers: dict[str, str] | None = None,
) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.5",
            **(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        raise HttpError(f"GET {url} failed: {exc}") from exc


def get_json(
    url: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """GET с разбором JSON. В ошибке сохраняется HTTP-статус.

    ``get_bytes`` статус не отдаёт, а политика повторов у платного API строится
    именно на нём: 402 повторять бессмысленно, 429 и 5xx — нужно.
    """

    query = urllib.parse.urlencode(
        {key: value for key, value in (params or {}).items() if value is not None}
    )
    request = urllib.request.Request(
        f"{url}?{query}" if query else url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Тело ответа в сообщение не попадает: в нём может оказаться эхо
        # запроса вместе с ключом.
        raise HttpError(f"GET {url} failed with HTTP {exc.code}", exc.code) from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise HttpError(f"GET {url} failed: {type(exc).__name__}") from exc


def post_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: int = 90) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT, **headers},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise HttpError(f"POST {url} failed with HTTP {exc.code}: {detail}", exc.code) from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise HttpError(f"POST {url} failed: {exc}") from exc


def post_form(url: str, fields: dict[str, Any], timeout: int = 30) -> dict[str, Any]:
    encoded = urllib.parse.urlencode({key: str(value) for key, value in fields.items()}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=encoded,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise HttpError(f"POST {url} failed with HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise HttpError(f"POST {url} failed: {exc}") from exc
    if not result.get("ok"):
        raise HttpError(f"API error from {url}: {result.get('description', result)}")
    return result
