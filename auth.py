import json
import logging
import os
import threading
import time
from typing import Any

import anyio
import jwt
import requests
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import Response as StarletteResponse

logger = logging.getLogger(__name__)

# 기본 보안/성능 설정
_DEFAULT_JWKS_CACHE_TTL_SEC = 600.0
_DEFAULT_JWKS_STALE_TTL_SEC = 3600.0
_DEFAULT_JWKS_HTTP_TIMEOUT_SEC = 2.0
_DEFAULT_RATE_LIMIT_WINDOW_SEC = 60.0
_DEFAULT_RATE_LIMIT_MAX_REQUESTS = 120


def _get_float_env(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
        if value <= minimum:
            raise ValueError
        return value
    except ValueError:
        logger.warning("Invalid %s=%r; using default=%s", name, raw, default)
        return default


def _get_int_env(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
        if value < minimum:
            raise ValueError
        return value
    except ValueError:
        logger.warning("Invalid %s=%r; using default=%s", name, raw, default)
        return default


_JWKS_CACHE_TTL_SEC = _get_float_env(
    "JWKS_CACHE_TTL_SEC", _DEFAULT_JWKS_CACHE_TTL_SEC
)
_JWKS_STALE_TTL_SEC = _get_float_env(
    "JWKS_STALE_TTL_SEC", _DEFAULT_JWKS_STALE_TTL_SEC
)
_JWKS_HTTP_TIMEOUT_SEC = _get_float_env(
    "JWKS_HTTP_TIMEOUT_SEC", _DEFAULT_JWKS_HTTP_TIMEOUT_SEC
)
_RATE_LIMIT_WINDOW_SEC = _get_float_env(
    "MCP_RATE_LIMIT_WINDOW_SEC", _DEFAULT_RATE_LIMIT_WINDOW_SEC
)
_RATE_LIMIT_MAX_REQUESTS = _get_int_env(
    "MCP_RATE_LIMIT_MAX_REQUESTS", _DEFAULT_RATE_LIMIT_MAX_REQUESTS
)

# JWKS 캐시 (키 재요청 최소화 + stale fallback)
_jwks_cache: dict[str, Any] = {}
_jwks_cache_expire_at: float = 0
_jwks_cache_stale_until: float = 0
_jwks_refresh_in_progress = False
_jwks_lock = threading.Lock()

# 인메모리 레이트 리미팅 (프로세스 단위)
_rate_limit_store: dict[str, tuple[float, int]] = {}
_rate_limit_lock = threading.Lock()


class ScopeValidationError(Exception):
    """요청 토큰에 필요한 scope가 없는 경우."""


def get_xsuaa_credentials() -> dict[str, Any]:
    """VCAP_SERVICES에서 XSUAA 크레덴셜을 추출한다."""
    raw_vcap = os.environ.get("VCAP_SERVICES", "{}")
    try:
        vcap = json.loads(raw_vcap)
    except json.JSONDecodeError:
        logger.error("Invalid VCAP_SERVICES JSON")
        return {}
    xsuaa_list = vcap.get("xsuaa", [])
    if not xsuaa_list:
        return {}
    return xsuaa_list[0].get("credentials", {})


def _fetch_jwks(xsuaa_url: str, force_refresh: bool = False) -> dict[str, Any]:
    """XSUAA에서 JWKS(공개 키 세트)를 가져온다.

    캐시 만료 시 동기 네트워크 호출이 필요하지만,
    실패하면 일정 시간 stale 캐시를 사용해 tail-latency/가용성 저하를 완화한다.
    """
    global _jwks_cache, _jwks_cache_expire_at, _jwks_cache_stale_until

    now = time.time()
    if not force_refresh and _jwks_cache and now < _jwks_cache_expire_at:
        return _jwks_cache
    if (
        not force_refresh
        and _jwks_cache
        and _jwks_cache_expire_at <= now < _jwks_cache_stale_until
    ):
        _trigger_jwks_refresh(xsuaa_url)
        return _jwks_cache

    with _jwks_lock:
        now = time.time()
        if not force_refresh and _jwks_cache and now < _jwks_cache_expire_at:
            return _jwks_cache

        jwks_url = f"{xsuaa_url.rstrip('/')}/token_keys"
        try:
            resp = requests.get(jwks_url, timeout=_JWKS_HTTP_TIMEOUT_SEC)
            resp.raise_for_status()
            fresh_jwks = resp.json()
            if not isinstance(fresh_jwks, dict) or "keys" not in fresh_jwks:
                raise ValueError("Invalid JWKS payload")

            _jwks_cache = fresh_jwks
            _jwks_cache_expire_at = now + _JWKS_CACHE_TTL_SEC
            _jwks_cache_stale_until = now + _JWKS_STALE_TTL_SEC
            return _jwks_cache
        except Exception:
            if _jwks_cache and now < _jwks_cache_stale_until:
                logger.warning("JWKS refresh failed; using stale JWKS cache")
                return _jwks_cache
            raise


def _trigger_jwks_refresh(xsuaa_url: str) -> None:
    """stale 캐시를 즉시 반환하고, JWKS 갱신은 백그라운드에서 수행한다."""
    global _jwks_refresh_in_progress

    with _jwks_lock:
        if _jwks_refresh_in_progress:
            return
        _jwks_refresh_in_progress = True

    def _run() -> None:
        global _jwks_refresh_in_progress
        try:
            _fetch_jwks(xsuaa_url, force_refresh=True)
        except Exception:
            logger.warning("Background JWKS refresh failed", exc_info=True)
        finally:
            with _jwks_lock:
                _jwks_refresh_in_progress = False

    threading.Thread(target=_run, daemon=True, name="jwks-refresh").start()


def _get_signing_key(jwks: dict[str, Any], token_header: dict[str, Any]) -> jwt.PyJWK:
    """토큰 헤더의 kid와 매칭되는 서명 키를 찾는다."""
    kid = token_header.get("kid")
    for key_data in jwks.get("keys", []):
        if key_data.get("kid") == kid:
            return jwt.PyJWK(key_data)
    raise ValueError(f"Matching key not found for kid: {kid}")


def _extract_scopes(payload: dict[str, Any]) -> set[str]:
    raw_scopes = payload.get("scope")
    if raw_scopes is None:
        raw_scopes = payload.get("scp")

    scopes: set[str] = set()
    if isinstance(raw_scopes, str):
        scopes.update([item for item in raw_scopes.split() if item])
    elif isinstance(raw_scopes, list):
        scopes.update([str(item) for item in raw_scopes if item])
    return scopes


def _required_scopes(xsuaa_creds: dict[str, Any]) -> list[str]:
    configured = os.environ.get("MCP_REQUIRED_SCOPES", "").strip()
    if configured:
        return [item.strip() for item in configured.split(",") if item.strip()]

    xsappname = str(xsuaa_creds.get("xsappname", "")).strip()
    if xsappname:
        return [f"{xsappname}.mcp_access"]
    return []


def _expected_issuers(xsuaa_creds: dict[str, Any]) -> set[str]:
    configured = os.environ.get("MCP_ALLOWED_ISSUERS", "").strip()
    if configured:
        return {item.strip().rstrip("/") for item in configured.split(",") if item.strip()}

    xsuaa_url = str(xsuaa_creds.get("url", "")).strip().rstrip("/")
    if not xsuaa_url:
        return set()
    return {xsuaa_url, f"{xsuaa_url}/oauth/token"}


def _has_local_scope(
    token_scopes: set[str], xsappname: str, local_scope_name: str
) -> bool:
    """XSUAA 로컬 스코프 매칭. !tNNNNN 접미사 패턴을 처리한다.

    XSUAA는 런타임에 스코프 이름을 '{xsappname}!tNNNNN.{scope}' 형태로 정규화하지만,
    VCAP_SERVICES의 xsappname에는 접미사가 없을 수 있다.
    """
    qualified = f"{xsappname}.{local_scope_name}"
    if qualified in token_scopes:
        return True
    suffix = f".{local_scope_name}"
    return any(
        scope.endswith(suffix)
        and (
            scope[: -len(suffix)] == xsappname
            or scope[: -len(suffix)].startswith(f"{xsappname}!t")
        )
        for scope in token_scopes
    )


def _validate_required_scope(payload: dict[str, Any], xsuaa_creds: dict[str, Any]) -> None:
    required = _required_scopes(xsuaa_creds)
    if not required:
        logger.warning("No required scope configured; skipping scope check")
        return

    token_scopes = _extract_scopes(payload)
    xsappname = str(xsuaa_creds.get("xsappname", "")).strip()

    logger.debug(
        "Scope check — required=%s token_scopes=%s xsappname=%r",
        required,
        sorted(token_scopes),
        xsappname,
    )

    # VCAP xsappname에서 !tNNNNN 접미사를 제거한 기본 이름 (MCP_REQUIRED_SCOPES가
    # 접미사 없이 설정된 경우에도 매칭할 수 있도록 양쪽 모두 시도한다)
    base_xsappname = xsappname.split("!")[0] if "!" in xsappname else xsappname

    for scope in required:
        # 직접 매칭 (완전히 동일한 스코프 문자열)
        if scope in token_scopes:
            continue
        # xsappname 기반 로컬 스코프 매칭: 전체 이름과 기본 이름 모두 시도
        for app_name in {xsappname, base_xsappname} - {""}:
            if scope.startswith(f"{app_name}."):
                local_name = scope[len(app_name) + 1 :]
                if _has_local_scope(token_scopes, base_xsappname, local_name):
                    break
        else:
            logger.warning(
                "Scope check FAILED — missing=%r required=%s token_scopes=%s",
                scope,
                required,
                sorted(token_scopes),
            )
            raise ScopeValidationError("Missing required scope")


def _extract_client_ip_from_request(request: StarletteRequest) -> str:
    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for:
        first_ip = forwarded_for.split(",")[0].strip()
        if first_ip:
            return first_ip
    client = request.client
    return client.host if client else "unknown"


def _check_rate_limit(client_key: str) -> int | None:
    """고정 윈도우 방식 레이트 리미팅. 초과 시 retry-after(초) 반환."""
    now = time.time()
    with _rate_limit_lock:
        window_started_at, count = _rate_limit_store.get(client_key, (now, 0))
        if now - window_started_at >= _RATE_LIMIT_WINDOW_SEC:
            window_started_at = now
            count = 0

        count += 1
        _rate_limit_store[client_key] = (window_started_at, count)

        # 메모리 보호: 오래된 키를 주기적으로 정리
        if len(_rate_limit_store) > 20000:
            cutoff = now - (_RATE_LIMIT_WINDOW_SEC * 2)
            stale_keys = [
                key for key, (started_at, _) in _rate_limit_store.items() if started_at < cutoff
            ]
            for stale_key in stale_keys:
                _rate_limit_store.pop(stale_key, None)

        if count > _RATE_LIMIT_MAX_REQUESTS:
            retry_after = max(1, int(_RATE_LIMIT_WINDOW_SEC - (now - window_started_at)))
            return retry_after
    return None


def _json_response(message: str, status: int, retry_after: int | None = None) -> StarletteResponse:
    headers = {}
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return StarletteResponse(
        json.dumps({"error": message}),
        status_code=status,
        headers=headers,
        media_type="application/json",
    )


def validate_token(token: str, xsuaa_creds: dict[str, Any]) -> dict[str, Any]:
    """JWT 토큰을 XSUAA 공개 키로 검증하고 payload를 반환한다."""
    xsuaa_url = str(xsuaa_creds.get("url", "")).strip()
    client_id = xsuaa_creds.get("clientid", "")
    expected_issuers = _expected_issuers(xsuaa_creds)

    if not xsuaa_url:
        raise jwt.InvalidTokenError("XSUAA URL is missing")

    token_header = jwt.get_unverified_header(token)

    jwks = _fetch_jwks(xsuaa_url)
    try:
        signing_key = _get_signing_key(jwks, token_header)
    except ValueError:
        jwks = _fetch_jwks(xsuaa_url, force_refresh=True)
        signing_key = _get_signing_key(jwks, token_header)

    payload = jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        audience=client_id,
        issuer=expected_issuers if expected_issuers else None,
        options={
            "verify_aud": bool(client_id),
            "verify_iss": bool(expected_issuers),
        },
    )
    _validate_required_scope(payload, xsuaa_creds)
    return payload


class XSUAAAuthMiddleware(BaseHTTPMiddleware):
    """Starlette 미들웨어: XSUAA JWT 인증.

    VCAP_SERVICES에 XSUAA가 없으면 (로컬 개발) 인증을 건너뛴다.
    /health 경로는 인증 없이 통과시킨다.
    """

    async def dispatch(self, request: StarletteRequest, call_next):
        if request.url.path == "/health":
            return await call_next(request)

        retry_after = _check_rate_limit(f"ip:{_extract_client_ip_from_request(request)}")
        if retry_after is not None:
            return _json_response("Too many requests", status=429, retry_after=retry_after)

        xsuaa_creds = get_xsuaa_credentials()

        if not xsuaa_creds:
            logger.warning("XSUAA not bound — skipping authentication (local dev mode)")
            if os.environ.get("VCAP_APPLICATION"):
                return _json_response("Authentication configuration error", status=401)
            return await call_next(request)

        # 임시 API Key 인증 (테스트용 — MCP_API_KEY 환경변수가 설정된 경우에만 활성화)
        api_key_env = os.environ.get("MCP_API_KEY", "").strip()
        if api_key_env:
            request_api_key = request.headers.get("x-api-key", "")
            if request_api_key == api_key_env:
                return await call_next(request)

        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return _json_response("Missing or invalid Authorization header", status=401)

        token = auth_header[len("Bearer "):]

        try:
            await anyio.to_thread.run_sync(lambda: validate_token(token, xsuaa_creds))
        except jwt.ExpiredSignatureError:
            return _json_response("Token expired", status=401)
        except ScopeValidationError:
            return _json_response("Forbidden", status=403)
        except jwt.InvalidTokenError:
            return _json_response("Invalid token", status=401)
        except Exception:
            logger.exception("Token validation failed")
            return _json_response("Authentication failed", status=401)

        return await call_next(request)
