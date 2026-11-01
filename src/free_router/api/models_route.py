"""``GET /v1/models``: the models this router will actually serve.

The listing is a **projection of configuration** (``spec.md`` section 5). There is
no discovery call, no cache to invalidate and no second source of truth: the ids
are ``config.models``, the same list the dispatcher routes over, so "listed" and
"routable" cannot drift apart.

Three properties are decisions rather than defaults (B5):

* ``created`` is the configuration file's **modification time**, captured once at
  load. A config-declared model has no creation date, and the alternative --
  stamping "now" -- would make every re-fetch look like a model update.
* ``owned_by`` is the **deployment owner**, not the provider. The provider is
  already carried by the model id (``google/...``, ``mistral/...``).
* ``permission`` and ``default`` are **emitted** even though the router grants no
  permissions and has no default model. Some clients read those keys and behave
  inconsistently when they are absent.

**A cooling model is still listed** (T054). Availability travels in its own
field, so transient provider state changes a *value* rather than the *shape* of
the list. A client that re-fetches during a rate-limit window sees the same ids
in the same order.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from free_router.api.routes import get_config, get_dispatcher
from free_router.core.config import Config, ModelConfig
from free_router.core.dispatch import Dispatcher

router = APIRouter(prefix="/v1", tags=["openai"])


class ModelObject(BaseModel):
    """One model in the OpenAI model-object shape, plus availability."""

    id: str
    object: Literal["model"] = "model"
    created: int
    owned_by: str
    permission: list[dict[str, Any]] = Field(default_factory=list)
    """Always empty.

    The router has no permission objects to grant. The key is present because
    omitting it changes client behaviour (B5), not because the value is useful.
    """
    default: bool | None = None
    """Always ``None``: the router has no notion of a default model."""
    available: bool
    """Whether this model could be dispatched right now (T054).

    ``False`` while the model is cooling down after a rate limit. It is reported
    *alongside* the entry rather than by removing it: hiding a cooling model
    would make a client's model list oscillate with provider state, and a client
    that re-fetches on that basis would flip-flop between requests.

    **Only cooldown is reflected.** Quota exhaustion and key depletion are not.
    Those are per-request capacity decisions the dispatcher makes against a
    *specific* estimate, so projecting them here would require guessing the
    estimate and would make the listing change on nearly every request -- the
    oscillation this field exists to avoid. A model marked available is one that
    is not rate-limited; whether a particular request fits its remaining budget
    is still decided per request.
    """


class ModelList(BaseModel):
    """The OpenAI list envelope."""

    object: Literal["list"] = "list"
    data: list[ModelObject]


def _model_object(entry: ModelConfig, config: Config, dispatcher: Dispatcher) -> ModelObject:
    """Project one configured model into its response shape."""
    return ModelObject(
        id=entry.id,
        created=config.config_mtime,
        owned_by=config.owned_by,
        # Keyed on the provider-native id: cooldowns are recorded against the
        # identifier the provider uses, not the external ``provider/...`` one.
        available=not dispatcher.cooldown.is_cooling(entry.provider, entry.provider_id),
    )


@router.get("/models")
async def list_models(
    config: Config = Depends(get_config),
    dispatcher: Dispatcher = Depends(get_dispatcher),
) -> ModelList:
    """List every configured model, whether or not it is currently usable."""
    return ModelList(
        data=[_model_object(entry, config, dispatcher) for entry in config.models],
    )
