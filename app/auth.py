import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt
from fastapi import Request
from psycopg.errors import UniqueViolation
from pwdlib import PasswordHash

from app.config import settings
from app.db import transaction
from app.errors import ApiError

password_hash = PasswordHash.recommended()
DUMMY_HASH = password_hash.hash("dummy-password-for-constant-login-work")
ISSUER = "marketplace-api"
AUDIENCE = "marketplace-client"


def require_role(user, *roles):
    if user["role"] not in roles:
        raise ApiError("ACCESS_DENIED", 403, "Role does not permit this operation")


def register(body):
    try:
        with transaction() as conn:
            return conn.execute(
                "INSERT INTO users(username,password_hash,role) VALUES (%s,%s,%s) RETURNING id,username,role",
                (body.username, password_hash.hash(body.password.get_secret_value()), body.role.value),
            ).fetchone()
    except UniqueViolation:
        raise ApiError("USER_ALREADY_EXISTS", 409, "Username is already registered") from None


def issue_tokens(conn, user):
    config = settings()
    now = datetime.now(UTC)
    common = {"sub": str(user["id"]), "role": user["role"], "iss": ISSUER, "aud": AUDIENCE, "iat": now}
    access = jwt.encode(
        common
        | {
            "token_type": "access",
            "jti": str(uuid4()),
            "exp": now + timedelta(minutes=config.access_minutes),
        },
        config.jwt_secret,
        algorithm="HS256",
    )
    token_id = uuid4()
    expiry = now + timedelta(days=config.refresh_days)
    refresh = jwt.encode(
        common | {"token_type": "refresh", "jti": str(token_id), "exp": expiry},
        config.jwt_secret,
        algorithm="HS256",
    )
    conn.execute(
        "INSERT INTO refresh_tokens(id,user_id,token_hash,expires_at) VALUES (%s,%s,%s,%s)",
        (token_id, user["id"], hashlib.sha256(refresh.encode()).hexdigest(), expiry),
    )
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "bearer",
        "expires_in": config.access_minutes * 60,
    }


def login(body):
    with transaction() as conn:
        user = conn.execute("SELECT * FROM users WHERE username=%s", (body.username,)).fetchone()
        valid = password_hash.verify(
            body.password.get_secret_value(), user["password_hash"] if user else DUMMY_HASH
        )
        if not user or not valid:
            raise ApiError("INVALID_CREDENTIALS", 401, "Invalid username or password")
        return issue_tokens(conn, user)


def decode_token(token, expected):
    try:
        claims = jwt.decode(
            token,
            settings().jwt_secret,
            algorithms=["HS256"],
            issuer=ISSUER,
            audience=AUDIENCE,
            options={"require": ["sub", "role", "exp", "iat", "jti", "token_type"]},
        )
        if claims["token_type"] != expected:
            raise ValueError("Wrong token type")
        UUID(claims["sub"])
        UUID(claims["jti"])
        return claims
    except (jwt.InvalidTokenError, ValueError, TypeError, KeyError) as exc:
        if expected == "refresh":
            code = "REFRESH_TOKEN_INVALID"
        else:
            code = "TOKEN_EXPIRED" if isinstance(exc, jwt.ExpiredSignatureError) else "TOKEN_INVALID"
        raise ApiError(code, 401, "Token is expired or invalid") from None


def refresh(body):
    claims = decode_token(body.refresh_token, "refresh")
    with transaction() as conn:
        token = conn.execute(
            "SELECT * FROM refresh_tokens WHERE id=%s FOR UPDATE", (claims["jti"],)
        ).fetchone()
        token_hash = hashlib.sha256(body.refresh_token.encode()).hexdigest()
        if (
            not token
            or token["revoked"]
            or token["token_hash"] != token_hash
            or str(token["user_id"]) != claims["sub"]
            or token["expires_at"] <= datetime.now(UTC)
        ):
            raise ApiError("REFRESH_TOKEN_INVALID", 401, "Refresh token is invalid or revoked")
        user = conn.execute("SELECT * FROM users WHERE id=%s", (claims["sub"],)).fetchone()
        conn.execute("UPDATE refresh_tokens SET revoked=TRUE WHERE id=%s", (token["id"],))
        return issue_tokens(conn, user)


def current_user(request: Request):
    authorization = request.headers.get("Authorization", "").split()
    if len(authorization) != 2 or authorization[0].lower() != "bearer":
        raise ApiError("TOKEN_INVALID", 401, "Bearer access token is required")
    claims = decode_token(authorization[1], "access")
    with transaction() as conn:
        user = conn.execute("SELECT id,username,role FROM users WHERE id=%s", (claims["sub"],)).fetchone()
        if not user:
            raise ApiError("TOKEN_INVALID", 401, "Token user does not exist")
    request.state.user_id = str(user["id"])
    return user
