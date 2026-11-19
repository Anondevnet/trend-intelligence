"""How requests reach Anthropic. Offline by default; the real SDK transport is built only when
every live switch is on. Responses are normalised to plain dicts so mocks stay simple."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, cast

from liquidmexx.config import ModelsConfig
from liquidmexx.settings import Settings


class LiveCallsDisabledError(RuntimeError):
    """A real API call was attempted while live calls are off."""


class Transport(Protocol):
    def create(self, params: dict[str, Any]) -> dict[str, Any]: ...
    def create_batch(self, requests: Sequence[dict[str, Any]]) -> str: ...
    def batch_status(self, batch_id: str) -> str: ...
    def batch_results(self, batch_id: str) -> list[dict[str, Any]]: ...


class OfflineTransport:
    """The default. Refuses every call, so nothing can reach the network by accident."""

    reason = ("real Anthropic calls are off: they need live_calls: true in models.yaml, "
              "LIQUIDMEXX_LIVE_LLM=enabled and an ANTHROPIC_API_KEY")

    def create(self, params: dict[str, Any]) -> dict[str, Any]:
        raise LiveCallsDisabledError(self.reason)

    def create_batch(self, requests: Sequence[dict[str, Any]]) -> str:
        raise LiveCallsDisabledError(self.reason)

    def batch_status(self, batch_id: str) -> str:
        raise LiveCallsDisabledError(self.reason)

    def batch_results(self, batch_id: str) -> list[dict[str, Any]]:
        raise LiveCallsDisabledError(self.reason)


class AnthropicTransport:  # pragma: no cover - never exercised without explicit approval
    """Thin wrapper over the official SDK. Constructed only by ``make_transport``."""

    def __init__(self, api_key: str) -> None:
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key)

    def create(self, params: dict[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._client.messages.create(**params).to_dict())

    def create_batch(self, requests: Sequence[dict[str, Any]]) -> str:
        from anthropic.types.messages.batch_create_params import Request

        batch = self._client.messages.batches.create(
            requests=[cast(Request, r) for r in requests])
        return batch.id

    def batch_status(self, batch_id: str) -> str:
        return str(self._client.messages.batches.retrieve(batch_id).processing_status)

    def batch_results(self, batch_id: str) -> list[dict[str, Any]]:
        return [cast(dict[str, Any], r.to_dict())
                for r in self._client.messages.batches.results(batch_id)]


def live_allowed(models: ModelsConfig, settings: Settings) -> bool:
    key = settings.anthropic_api_key
    return (models.live_calls and settings.live_llm_opt_in
            and key is not None and bool(key.get_secret_value()))


def make_transport(models: ModelsConfig, settings: Settings) -> Transport:
    if not live_allowed(models, settings):
        return OfflineTransport()
    assert settings.anthropic_api_key is not None
    return AnthropicTransport(settings.anthropic_api_key.get_secret_value())  # pragma: no cover
