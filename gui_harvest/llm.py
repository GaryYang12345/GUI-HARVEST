"""Official optimizer-model clients.

Supported endpoints are the OpenAI API and OpenRouter. Role model identifiers
come from ``gui-harvest.toml``; this module only resolves credentials and the
public base URL.
"""

from __future__ import annotations

import os

from openai import OpenAI

BACKENDS = {
    "openai": {
        "name": "openai",
        "base_url_env": "OPENAI_BASE_URL",
        "base_url_default": "https://api.openai.com/v1",
        "api_key_envs": ("OPENAI_API_KEY",),
        "supports_provider_routing": False,
    },
    "openrouter": {
        "name": "openrouter",
        "base_url_env": "OPENROUTER_BASE_URL",
        "base_url_default": "https://openrouter.ai/api/v1",
        "api_key_envs": ("OPENROUTER_API_KEY",),
        "supports_provider_routing": True,
    },
}


def resolve_backend(name=None):
    raw = (
        name
        or os.environ.get("GUI_HARVEST_LLM_BACKEND")
        or "openrouter"
    ).strip().lower()
    if raw not in BACKENDS:
        raise SystemExit(
            f"unknown GUI-HARVEST backend {raw!r}; choose one of "
            f"{sorted(BACKENDS)}"
        )
    return BACKENDS[raw]


def _api_key(cfg):
    for env in cfg["api_key_envs"]:
        val = os.environ.get(env)
        if val:
            return val, env
    return None, cfg["api_key_envs"][0]


def make_client(backend=None):
    cfg = resolve_backend(backend)
    key, key_env = _api_key(cfg)
    if not key:
        raise SystemExit(
            f"backend {cfg['name']} needs {key_env} in the environment"
        )
    return OpenAI(
        base_url=os.environ.get(cfg["base_url_env"], cfg["base_url_default"]),
        api_key=key,
    )


def extra_body_for(provider_order, allow_fallbacks, backend=None):
    """OpenRouter-only `provider` routing. Other backends get None."""
    cfg = resolve_backend(backend)
    if not cfg["supports_provider_routing"] or not provider_order:
        return None
    return {
        "provider": {
            "order": [p.strip() for p in provider_order.split(",") if p.strip()],
            "allow_fallbacks": bool(allow_fallbacks),
        }
    }


def client_config(backend=None):
    """Fields to stamp into every role's config so a backend flip is auditable."""
    cfg = resolve_backend(backend)
    _, key_env = _api_key(cfg)
    return {
        "backend": cfg["name"],
        "base_url": os.environ.get(cfg["base_url_env"], cfg["base_url_default"]),
        "api_key_env": key_env,
        "supports_provider_routing": cfg["supports_provider_routing"],
    }
