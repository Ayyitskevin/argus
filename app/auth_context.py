"""Request auth context for homelab bearer and Phase 10 SaaS tenants."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class AuthContext:
    is_admin: bool = False
    tenant: dict | None = None
    api_key_id: str | None = None

    @property
    def tenant_id(self) -> str | None:
        return self.tenant["id"] if self.tenant else None


_auth_ctx: ContextVar[AuthContext | None] = ContextVar("auth_ctx", default=None)


def set_auth_context(ctx: AuthContext | None) -> None:
    _auth_ctx.set(ctx)


def get_auth_context() -> AuthContext | None:
    return _auth_ctx.get()


def get_tenant_id() -> str | None:
    ctx = get_auth_context()
    return ctx.tenant_id if ctx else None


def is_admin() -> bool:
    ctx = get_auth_context()
    return bool(ctx and ctx.is_admin)