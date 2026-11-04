"""Autenticação da POC de controle de acesso — imita o repositório dos agentes.

Lá, o `mdsc-api-gateway` emite um JWT HS256 com `id`, `email` e `name`, e o backend o valida
com `AUTH_SECRET` (infra/security/jwt_auth.py). Aqui o próprio app faz o papel do gateway só
para a POC: `POST /auth/dev-login` emite o mesmo tipo de token para os usuários do `acl.yml`, e
um middleware valida o `Authorization: Bearer` das rotas protegidas e FORÇA o `user_id` do token
na requisição (query string e corpo JSON), ignorando o que o cliente mandou. Assim o resto do
app continua lendo `user_id` como sempre, mas agora ele não é mais forjável.

O papel (role) NÃO vai no token — como no repositório dos agentes, o token só identifica; o
papel é consultado no servidor (acesso.py). Sem controle de acesso ativo (`enabled: true` no
acl.yml do pack), o middleware não faz nada e o app se comporta como antes.

`EventSource` (SSE do chat) não envia headers: para ele o token também é aceito em `?access_token=`.
"""
import json
import logging
import os
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode

import jwt
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

import acesso

logger = logging.getLogger(__name__)

ALGORITHM = "HS256"
TOKEN_TTL_SECONDS = 8 * 3600
_DEV_SECRET = "dev-secret-poc-nao-usar-em-producao-0123456789"
_avisou_segredo = False

# Rotas que exigem token quando o controle de acesso está ativo. As demais (front estático,
# /app-info, /auth/*, e os downloads /chart /pdf /excel /reports, abertos por <img>/<a> que não
# mandam header e identificados por UUID) ficam abertas — limitação conhecida da POC.
PROTECTED_PREFIXES = (
    "/chat", "/tasks", "/artifacts", "/dashboard-widgets", "/alerts", "/chart-snapshot",
)


@dataclass(frozen=True, slots=True)
class CurrentUser:
    """Usuário autenticado, extraído do JWT (mesmos campos do repositório dos agentes)."""

    id: str
    email: str
    name: str


def _secret() -> str:
    secret = os.getenv("AUTH_SECRET")
    if not secret:
        global _avisou_segredo
        if not _avisou_segredo:
            _avisou_segredo = True
            logger.warning("AUTH_SECRET não definido — usando segredo de desenvolvimento da POC.")
        return _DEV_SECRET
    return secret


def create_token(user: CurrentUser, ttl: int = TOKEN_TTL_SECONDS) -> str:
    now = int(time.time())
    payload = {"id": user.id, "email": user.email, "name": user.name, "iat": now, "exp": now + ttl}
    return jwt.encode(payload, _secret(), algorithm=ALGORITHM)


def verify_token(token: str) -> CurrentUser:
    """Valida assinatura e expiração; levanta jwt.PyJWTError se inválido."""
    payload = jwt.decode(token, _secret(), algorithms=[ALGORITHM], options={"verify_aud": False})
    try:
        return CurrentUser(id=str(payload["id"]), email=str(payload["email"]), name=str(payload["name"]))
    except KeyError as exc:
        raise jwt.InvalidTokenError("token sem id/email/name") from exc


def dev_login(user_id: str) -> "tuple[str, CurrentUser] | None":
    """Emite um token para um usuário do acl.yml (o 'login' do gateway simulado)."""
    info = acesso.usuario(user_id)
    if info is None:
        return None
    user = CurrentUser(id=str(user_id), email=info.get("email", ""), name=info.get("name", str(user_id)))
    return create_token(user), user


def _unauthorized(message: str) -> JSONResponse:
    return JSONResponse(status_code=401, content={"error": True, "status": 401, "message": message})


def _bearer(scope: Scope, query: "list[tuple[str, str]]") -> "str | None":
    for k, v in scope.get("headers", []):
        if k == b"authorization":
            valor = v.decode("latin-1")
            if valor.lower().startswith("bearer "):
                return valor[7:].strip()
    return next((v for k, v in query if k == "access_token"), None)


class AuthMiddleware:
    """Valida o JWT das rotas protegidas e impõe o `user_id` do token na requisição."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] == "OPTIONS"
            or not scope["path"].startswith(PROTECTED_PREFIXES)
            or not acesso.ativo()
        ):
            return await self.app(scope, receive, send)

        query = parse_qsl(scope.get("query_string", b"").decode(), keep_blank_values=True)
        token = _bearer(scope, query)
        if not token:
            return await _unauthorized("Token de autenticação ausente.")(scope, receive, send)
        try:
            user = verify_token(token)
        except jwt.PyJWTError:
            return await _unauthorized("Token inválido ou expirado.")(scope, receive, send)

        # user_id da query: o do token, sempre (e sem o access_token, que não precisa ir adiante).
        query = [(k, v) for k, v in query if k not in ("user_id", "access_token")] + [("user_id", user.id)]
        scope = {**scope, "query_string": urlencode(query).encode()}
        scope.setdefault("state", {})["user_id"] = user.id

        if scope["method"] in ("POST", "PUT", "PATCH") and _is_json(scope):
            receive = await _json_body_with_user(receive, user.id)
        await self.app(scope, receive, send)


def _is_json(scope: Scope) -> bool:
    return any(k == b"content-type" and b"application/json" in v for k, v in scope.get("headers", []))


async def _json_body_with_user(receive: Receive, user_id: str) -> Receive:
    """Lê o corpo JSON inteiro, troca `user_id` pelo do token e devolve um `receive` que o reentrega."""
    chunks, more = [], True
    while more:
        msg = await receive()
        chunks.append(msg.get("body", b""))
        more = msg.get("more_body", False)
    corpo = b"".join(chunks)
    try:
        dados = json.loads(corpo) if corpo else {}
        if isinstance(dados, dict):
            dados["user_id"] = user_id
            corpo = json.dumps(dados).encode()
    except ValueError:
        pass  # corpo inválido: deixa o FastAPI responder 422 como sempre

    entregue = False

    async def novo_receive() -> Message:
        nonlocal entregue
        if entregue:
            return await receive()
        entregue = True
        return {"type": "http.request", "body": corpo, "more_body": False}

    return novo_receive
