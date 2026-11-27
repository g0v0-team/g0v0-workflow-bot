from base64 import urlsafe_b64encode
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import re
from secrets import token_urlsafe
from sqlite3 import SQLITE_CONSTRAINT_UNIQUE
from urllib.parse import urlencode

from src.service.github.models import GitHubAccountBinding, GitHubOAuthState

from aiohttp import ClientError, ClientSession, ClientTimeout
import nonebot
from nonebot import on_command
from nonebot.adapters.onebot.v11 import PrivateMessageEvent
from nonebot.drivers import URL, ASGIMixin, HTTPServerSetup, Request, Response
from nonebot.log import logger
from nonebot.rule import is_type
from nonebot_plugin_orm import Model, get_session
from pydantic import AnyHttpUrl, BaseModel, SecretStr, field_validator
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

OAUTH_CALLBACK_PATH = "/gh-oauth"


class GitHubOAuthConfig(BaseModel):
    github_oauth_client_id: str | None = None
    github_oauth_client_secret: SecretStr | None = None
    github_oauth_base_url: AnyHttpUrl = AnyHttpUrl("https://example.com")

    @field_validator("github_oauth_base_url")
    @classmethod
    def validate_base_url(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if any(part is not None for part in (value.username, value.password, value.query, value.fragment)):
            raise ValueError("GitHub OAuth base URL cannot include credentials, a query, or a fragment")
        return value

    @property
    def callback_url(self) -> str:
        return str(self.github_oauth_base_url).rstrip("/") + OAUTH_CALLBACK_PATH


config = nonebot.get_plugin_config(GitHubOAuthConfig)
driver = nonebot.get_driver()
ghbind = on_command("ghbind", rule=is_type(PrivateMessageEvent), force_whitespace=True, block=True)


def _insert(session: AsyncSession, model: type[Model]):
    if session.get_bind(mapper=model).dialect.name == "sqlite":
        return sqlite_insert(model)
    return postgres_insert(model)


@ghbind.handle()
async def begin_binding(event: PrivateMessageEvent) -> None:
    if not (config.github_oauth_client_id and config.github_oauth_client_secret and isinstance(driver, ASGIMixin)):
        await ghbind.finish("GitHub 账号绑定尚未配置。")

    state = token_urlsafe(32)
    verifier = token_urlsafe(64)
    now = datetime.now(UTC)
    values = {
        "state_hash": sha256(state.encode("ascii")).hexdigest(),
        "verifier": verifier,
        "expires_at": now + timedelta(minutes=10),
    }
    try:
        async with get_session() as session, session.begin():
            await session.execute(delete(GitHubOAuthState).where(GitHubOAuthState.expires_at <= now))
            statement = _insert(session, GitHubOAuthState).values(qq_id=event.user_id, **values)
            await session.execute(statement.on_conflict_do_update(index_elements=[GitHubOAuthState.qq_id], set_=values))
    except SQLAlchemyError as exc:
        logger.error("GitHub OAuth state storage failed: {}", type(exc).__name__)
        await ghbind.finish("暂时无法创建授权链接，请稍后重试。")

    challenge = urlsafe_b64encode(sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    params = urlencode(
        {
            "client_id": config.github_oauth_client_id,
            "redirect_uri": config.callback_url,
            "scope": "",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "prompt": "select_account",
        }
    )
    url = f"https://github.com/login/oauth/authorize?{params}"
    await ghbind.finish(f"请打开链接授权绑定 GitHub 账号。链接 10 分钟内有效，仅可使用一次，请勿转发。\n{url}")


async def _github_identity(
    code: str, verifier: str, client_id: str, client_secret: SecretStr
) -> tuple[int, str] | None:
    async with ClientSession(timeout=ClientTimeout(total=10)) as client:
        async with client.post(
            "https://github.com/login/oauth/access_token",
            data={
                "client_id": client_id,
                "client_secret": client_secret.get_secret_value(),
                "code": code,
                "redirect_uri": config.callback_url,
                "code_verifier": verifier,
            },
            headers={"Accept": "application/json"},
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                raise ValueError("Unexpected GitHub OAuth exchange status")
            grant = await response.json()
        if not isinstance(grant, dict) or grant.get("error"):
            return None
        token = grant.get("access_token")
        if not isinstance(token, str) or not token.startswith("gho_") or grant.get("scope") != "":
            return None

        async with client.get(
            "https://api.github.com/user",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2026-03-10",
            },
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                raise ValueError("Unexpected GitHub identity response status")
            profile = await response.json()
    github_id = profile.get("id") if isinstance(profile, dict) else None
    login = profile.get("login") if isinstance(profile, dict) else None
    if (
        type(github_id) is not int
        or not 0 < github_id < 2**63
        or not isinstance(login, str)
        or not 0 < len(login) <= 255
    ):
        raise ValueError("GitHub identity is missing a valid ID or login")
    return github_id, login


def _reply(message: str, status_code: int = 200) -> Response:
    return Response(
        status_code,
        content=message,
        headers={
            "Content-Type": "text/plain; charset=utf-8",
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
        },
    )


async def oauth_callback(request: Request) -> Response:
    client_id = config.github_oauth_client_id
    client_secret = config.github_oauth_client_secret
    if not client_id or not client_secret:
        return _reply("GitHub 账号绑定尚未配置。", 503)
    params = request.url.query
    state, code, error = params.get("state"), params.get("code"), params.get("error")
    if (
        any(len(params.getall(key, [])) > 1 for key in ("state", "code", "error"))
        or state is None
        or not re.fullmatch(r"[A-Za-z0-9_-]{43}", state)
        or (code is not None and len(code) > 1024)
        or (not code and error is None)
    ):
        return _reply("授权回调无效，请在 QQ 私聊中重新发送 /ghbind。", 400)

    try:
        async with get_session() as session, session.begin():
            result = await session.execute(
                delete(GitHubOAuthState)
                .where(
                    GitHubOAuthState.state_hash == sha256(state.encode("ascii")).hexdigest(),
                    GitHubOAuthState.expires_at > datetime.now(UTC),
                )
                .returning(GitHubOAuthState.qq_id, GitHubOAuthState.verifier)
            )
            pending = result.one_or_none()
        if pending is None:
            return _reply("授权链接已过期或已使用，请在 QQ 私聊中重新发送 /ghbind。", 400)
        if error is not None or not code:
            return _reply("GitHub 授权未完成，请在 QQ 私聊中重新发送 /ghbind。", 400)

        identity = await _github_identity(code, pending.verifier, client_id, client_secret)
        if identity is None:
            return _reply("GitHub 授权失败或授权权限超出身份验证范围，请重新发起绑定。", 400)
        github_id, login = identity
        async with get_session() as session, session.begin():
            values = {"github_id": github_id, "github_login": login}
            statement = _insert(session, GitHubAccountBinding).values(qq_id=pending.qq_id, **values)
            await session.execute(
                statement.on_conflict_do_update(index_elements=[GitHubAccountBinding.qq_id], set_=values)
            )
    except SQLAlchemyError as exc:
        if isinstance(exc, IntegrityError) and (
            getattr(exc.orig, "sqlstate", None) == "23505"
            or getattr(exc.orig, "sqlite_errorcode", None) == SQLITE_CONSTRAINT_UNIQUE
        ):
            return _reply("该 GitHub 账号已绑定到另一个 QQ 账号。", 409)
        logger.error("GitHub OAuth binding storage failed: {}", type(exc).__name__)
        return _reply("暂时无法保存绑定，请稍后在 QQ 私聊中重新发送 /ghbind。", 503)
    except (ClientError, TimeoutError, ValueError):
        return _reply("暂时无法从 GitHub 确认身份，请稍后在 QQ 私聊中重新发送 /ghbind。", 502)
    return _reply(f"GitHub 账号 @{login} 已绑定，可以返回 QQ。")


if isinstance(driver, ASGIMixin):
    driver.setup_http_server(HTTPServerSetup(URL(OAUTH_CALLBACK_PATH), "GET", "GitHub OAuth callback", oauth_callback))
