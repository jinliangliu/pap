# app/auth/tokens.py
"""Access Token 验签（HS256）。

为什么自己实现而不是引入 JWT 库：

- 校验逻辑不到 100 行，算法固定为 HS256，不存在算法协商，也就没有 `alg=none`
  或算法混淆的攻击面；
- 生产环境推荐的路径仍然是「可信身份网关完成 OIDC 验签 → 注入声明」
  （AUTH_MODE=gateway），本模块只服务于自托管部署里网关与平台共用密钥的场景。

关键约束：
- 只接受 `alg=HS256`，其他算法一律拒绝；
- `exp` 必须有；`nbf`/`iss`/`aud` 配了就校验；
- 签名比对使用 `hmac.compare_digest`，避免时序侧信道；
- Token 里的 roles/scopes **不参与授权**：授权一律以数据库为准，
  否则撤销一个角色要等到 Token 过期才生效。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any

from app.core.config import settings


class TokenError(Exception):
    """Token 无法通过校验。"""


class TokenExpired(TokenError):
    """Token 已过期。"""


@dataclass(frozen=True, slots=True)
class TokenClaims:
    subject: str
    tenant_id: str | None
    expires_at: int
    # 仅作参考，不用于授权判断。
    roles: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)

    try:
        return base64.urlsafe_b64decode(segment + padding)
    except (binascii.Error, ValueError) as exc:
        raise TokenError("Token 编码无效。") from exc


def _json_object(raw: bytes, *, what: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise TokenError(f"Token {what} 不是合法 JSON。") from exc

    if not isinstance(value, dict):
        raise TokenError(f"Token {what} 必须是对象。")

    return value


def _string_list(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        # OAuth 2 风格的 scope 是空格分隔的字符串。
        return tuple(part for part in value.split(" ") if part)
    if isinstance(value, list):
        return tuple(str(item) for item in value if isinstance(item, str))

    raise TokenError("Token 中的 roles/scopes 必须是字符串或字符串数组。")


def decode_access_token(
    token: str,
    *,
    secret: str | None = None,
    issuer: str | None = None,
    audience: str | None = None,
    leeway_seconds: int | None = None,
) -> TokenClaims:
    raw = (token or "").strip()

    if not raw:
        raise TokenError("缺少访问令牌。")

    parts = raw.split(".")

    if len(parts) != 3:
        raise TokenError("Token 结构无效。")

    header = _json_object(_b64url_decode(parts[0]), what="header")

    if header.get("alg") != "HS256":
        # 明确拒绝 none / RS256 / HS512：算法不在白名单内，不做降级尝试。
        raise TokenError("不支持的 Token 签名算法。")

    key = (secret if secret is not None else settings.jwt_secret) or ""

    if not key:
        raise TokenError("服务端未配置 JWT 密钥，无法验签。")

    signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
    expected = hmac.new(key.encode("utf-8"), signing_input, hashlib.sha256).digest()
    provided = _b64url_decode(parts[2])

    if not hmac.compare_digest(expected, provided):
        raise TokenError("Token 签名无效。")

    payload = _json_object(_b64url_decode(parts[1]), what="payload")

    now = int(time.time())
    slack = settings.jwt_leeway_seconds if leeway_seconds is None else leeway_seconds

    exp = payload.get("exp")

    if not isinstance(exp, (int, float)) or isinstance(exp, bool):
        raise TokenError("Token 缺少有效期。")

    if now > int(exp) + slack:
        raise TokenExpired("Token 已过期。")

    nbf = payload.get("nbf")

    if isinstance(nbf, (int, float)) and not isinstance(nbf, bool):
        if now + slack < int(nbf):
            raise TokenError("Token 尚未生效。")

    expected_issuer = issuer if issuer is not None else settings.jwt_issuer

    if expected_issuer and payload.get("iss") != expected_issuer:
        raise TokenError("Token 签发方不匹配。")

    expected_audience = audience if audience is not None else settings.jwt_audience

    if expected_audience:
        raw_aud = payload.get("aud")
        audiences = (
            [raw_aud]
            if isinstance(raw_aud, str)
            else list(raw_aud)
            if isinstance(raw_aud, list)
            else []
        )

        if expected_audience not in audiences:
            raise TokenError("Token 受众不匹配。")

    subject = payload.get("sub")

    if not isinstance(subject, str) or not subject.strip():
        raise TokenError("Token 缺少主体标识。")

    tenant_claim = payload.get("tenant_id") or payload.get("tenant")
    tenant_id = str(tenant_claim).strip() if tenant_claim else None

    return TokenClaims(
        subject=subject.strip(),
        tenant_id=tenant_id,
        expires_at=int(exp),
        roles=_string_list(payload.get("roles")),
        scopes=_string_list(payload.get("scopes") or payload.get("scope")),
    )


def encode_access_token(
    *,
    subject: str,
    tenant_id: str | None,
    secret: str,
    expires_in_seconds: int = 3600,
    issuer: str | None = None,
    audience: str | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    """仅供本地联调与测试签发 Token；生产由身份提供方签发。"""
    header = {"alg": "HS256", "typ": "JWT"}
    now = int(time.time())

    payload: dict[str, Any] = {
        "sub": subject,
        "iat": now,
        "exp": now + max(1, expires_in_seconds),
    }

    if tenant_id:
        payload["tenant_id"] = tenant_id
    if issuer:
        payload["iss"] = issuer
    if audience:
        payload["aud"] = audience
    if extra:
        payload.update(extra)

    def _segment(value: dict[str, Any]) -> str:
        raw = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    signing_input = f"{_segment(header)}.{_segment(payload)}"
    signature = hmac.new(
        secret.encode("utf-8"),
        signing_input.encode("ascii"),
        hashlib.sha256,
    ).digest()

    return f"{signing_input}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode('ascii')}"
