"""Async Metronome API client wrapper.

We use the official ``metronome-sdk`` Python package (imported as
``from metronome import AsyncMetronome``). This wrapper centralises:

  - construction of the SDK client (so the rest of the codebase does not
    repeat bearer-token / base-url plumbing),
  - the small set of operations we actually need (customer create / update,
    contract create, contract lookup by uniqueness key), and
  - classification of Metronome errors as **transient** (worth a retry) vs
    **permanent** (no retry).

If you need to call additional Metronome endpoints, add a method here rather
than importing ``AsyncMetronome`` directly elsewhere; the failure-classification
logic lives in one place that way.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from metronome import APIConnectionError as _MetronomeConnectionError
from metronome import APIError as _MetronomeAPIError
from metronome import APIStatusError as _MetronomeStatusError
from metronome import APITimeoutError as _MetronomeTimeoutError
from metronome import AsyncMetronome
from metronome import RateLimitError as _MetronomeRateLimitError

from sidecar.logging import get_logger

logger = get_logger(__name__)


# -----------------------------------------------------------------------------
# Our exception hierarchy. Handlers should only catch these, not the raw SDK
# exceptions, so that swapping the SDK out (or moving to httpx) does not ripple
# through the handler layer.
# -----------------------------------------------------------------------------


class MetronomeError(Exception):
    """Base class for all Metronome-side errors surfaced to the handler."""


class TransientMetronomeError(MetronomeError):
    """Network, 5xx, or 429 errors — safe to retry with backoff."""


class PermanentMetronomeError(MetronomeError):
    """4xx (other than 429) — the request will fail the same way next time.

    Carries an HTTP status code so callers can log it.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


# -----------------------------------------------------------------------------
# Result type for create. We could return the raw SDK response, but that ties
# every downstream caller to the SDK's pydantic models. A small dataclass is
# enough and is easy to construct in tests.
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CreatedCustomer:
    metronome_customer_id: str


@dataclass(frozen=True, slots=True)
class CreatedContract:
    """Result of a contract-create call.

    ``via_uniqueness_key_recovery`` is True iff Metronome returned 409 on
    creation and we recovered the existing contract ID via
    :meth:`MetronomeClient.find_contract_by_uniqueness_key`. The handler logs
    this so operators can see which contracts came from a retry-after-partial-
    success path vs a fresh create.
    """

    metronome_contract_id: str
    via_uniqueness_key_recovery: bool = False


class ContractNotFoundError(MetronomeError):
    """Raised when a uniqueness-key lookup finds nothing.

    This should be unreachable in normal operation: we only call
    :meth:`MetronomeClient.find_contract_by_uniqueness_key` after Metronome
    has just told us the uniqueness key collided. If the lookup then finds
    nothing, something is wrong on Metronome's side (or in our key
    derivation) and the handler treats it as a permanent failure.
    """


class MetronomeClient:
    """High-level operations against the Metronome API used by this sidecar."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.metronome.com",
        timeout_seconds: float = 30.0,
    ) -> None:
        # The SDK's AsyncMetronome speaks httpx under the hood, which means
        # ``respx`` can mock it in tests without any monkey-patching of this
        # class.
        self._sdk = AsyncMetronome(
            bearer_token=api_key,
            base_url=base_url,
            timeout=timeout_seconds,
        )

    async def aclose(self) -> None:
        """Release the underlying HTTP connection pool."""
        await self._sdk.close()

    # -------------------------------------------------------------------------
    # Operations
    # -------------------------------------------------------------------------

    async def create_customer(self, request_body: dict[str, Any]) -> CreatedCustomer:
        """Create a Metronome customer.

        ``request_body`` is the dict returned by
        :func:`sidecar.mappers.customer.stripe_customer_to_metronome_request`.

        We translate ``request_body`` into the SDK's keyword-argument shape
        (it expects ``**params``, not a single ``body=`` dict).
        """
        try:
            response = await self._sdk.v1.customers.create(**request_body)
        except _MetronomeStatusError as exc:
            raise self._classify_status_error(exc) from exc
        except (_MetronomeConnectionError, _MetronomeTimeoutError) as exc:
            raise TransientMetronomeError(str(exc)) from exc
        except _MetronomeAPIError as exc:
            # Catch-all for SDK errors that aren't covered above. Default to
            # transient: it's safer to retry an unknown failure than to give up.
            raise TransientMetronomeError(str(exc)) from exc

        # The CustomerCreateResponse exposes `.data.id` (string). We pull it
        # defensively in case the SDK shape evolves.
        customer_id = _extract_id(response)
        return CreatedCustomer(metronome_customer_id=customer_id)

    async def set_customer_name(self, *, metronome_customer_id: str, name: str) -> None:
        """Update the display name of an existing Metronome customer.

        We use ``set_name`` rather than a generic ``update`` because v0.1 only
        propagates the customer's display name. Extending this to propagate
        other fields (e.g. ``custom_fields``) is the natural next customization.
        """
        try:
            await self._sdk.v1.customers.set_name(
                customer_id=metronome_customer_id,
                name=name,
            )
        except _MetronomeStatusError as exc:
            raise self._classify_status_error(exc) from exc
        except (_MetronomeConnectionError, _MetronomeTimeoutError) as exc:
            raise TransientMetronomeError(str(exc)) from exc
        except _MetronomeAPIError as exc:
            raise TransientMetronomeError(str(exc)) from exc

    async def create_contract(self, request_body: dict[str, Any]) -> CreatedContract:
        """Create a Metronome contract.

        ``request_body`` is the dict returned by
        :func:`sidecar.mappers.subscription.stripe_subscription_to_metronome_contract_request`.
        The SDK accepts the same fields as ``**kwargs``.

        409 handling
        ------------
        Metronome's ``uniqueness_key`` field is documented to cause the create
        call to fail with HTTP 409 when a contract with that key already
        exists. The most common cause is a worker retry after a partial
        failure: a previous attempt successfully created the contract in
        Metronome but crashed before writing the local ``subscription_mappings``
        row.

        On 409 we transparently recover the existing contract's ID via
        :meth:`find_contract_by_uniqueness_key` and surface the result with
        ``via_uniqueness_key_recovery=True``. That keeps the handler's happy
        path single-branch: "I asked for a contract, I got an ID back".
        """
        try:
            response = await self._sdk.v1.contracts.create(**request_body)
        except _MetronomeStatusError as exc:
            status_code = getattr(exc, "status_code", None)
            if status_code == 409:
                customer_id = request_body.get("customer_id")
                uniqueness_key = request_body.get("uniqueness_key")
                if not isinstance(customer_id, str) or not isinstance(uniqueness_key, str):
                    # We require both to recover; if the caller skipped
                    # either there's no way to find the original contract.
                    raise PermanentMetronomeError(
                        "Metronome returned 409 but the request body is "
                        "missing customer_id or uniqueness_key — cannot "
                        "recover the existing contract ID.",
                        status_code=409,
                    ) from exc
                existing_id = await self.find_contract_by_uniqueness_key(
                    metronome_customer_id=customer_id,
                    uniqueness_key=uniqueness_key,
                )
                logger.info(
                    "metronome_contract_recovered_via_uniqueness_key",
                    extra={
                        "event": "metronome_contract_recovered_via_uniqueness_key",
                        "metronome_customer_id": customer_id,
                        "metronome_contract_id": existing_id,
                        "uniqueness_key": uniqueness_key,
                    },
                )
                return CreatedContract(
                    metronome_contract_id=existing_id,
                    via_uniqueness_key_recovery=True,
                )
            raise self._classify_status_error(exc) from exc
        except (_MetronomeConnectionError, _MetronomeTimeoutError) as exc:
            raise TransientMetronomeError(str(exc)) from exc
        except _MetronomeAPIError as exc:
            raise TransientMetronomeError(str(exc)) from exc

        contract_id = _extract_id(response)
        return CreatedContract(metronome_contract_id=contract_id)

    async def find_contract_by_uniqueness_key(
        self,
        *,
        metronome_customer_id: str,
        uniqueness_key: str,
    ) -> str:
        """Return the contract ID for a known ``uniqueness_key`` on a customer.

        Used to recover from a 409 on :meth:`create_contract`. The Metronome
        API does not expose a "get contract by uniqueness key" endpoint, so
        we list the customer's contracts and match on the field client-side.
        For a single SaaS customer this list is small (one row per
        active+historical subscription) so the cost is bounded.

        Raises
        ------
        ContractNotFoundError
            If no contract on this customer has the given uniqueness key.
            See the class docstring — this is a "shouldn't happen" path.
        TransientMetronomeError / PermanentMetronomeError
            Same classification rules as :meth:`create_contract` for any
            error surfacing from the list call itself.
        """
        try:
            response = await self._sdk.v1.contracts.list(customer_id=metronome_customer_id)
        except _MetronomeStatusError as exc:
            raise self._classify_status_error(exc) from exc
        except (_MetronomeConnectionError, _MetronomeTimeoutError) as exc:
            raise TransientMetronomeError(str(exc)) from exc
        except _MetronomeAPIError as exc:
            raise TransientMetronomeError(str(exc)) from exc

        for contract in _iter_contracts(response):
            if _get_attr(contract, "uniqueness_key") == uniqueness_key:
                contract_id = _get_attr(contract, "id")
                if isinstance(contract_id, str) and contract_id:
                    return contract_id

        raise ContractNotFoundError(
            f"No contract with uniqueness_key={uniqueness_key!r} found on "
            f"Metronome customer {metronome_customer_id!r}."
        )

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _classify_status_error(exc: _MetronomeStatusError) -> MetronomeError:
        """Map an SDK status error onto our transient/permanent split.

        - 429 Too Many Requests → transient
        - 5xx Server errors      → transient
        - Other 4xx              → permanent
        """
        status = getattr(exc, "status_code", None)
        if isinstance(exc, _MetronomeRateLimitError) or status == 429:
            return TransientMetronomeError(f"Metronome rate limited (HTTP {status}): {exc}")
        if isinstance(status, int) and 500 <= status < 600:
            return TransientMetronomeError(f"Metronome server error (HTTP {status}): {exc}")
        return PermanentMetronomeError(
            f"Metronome rejected request (HTTP {status}): {exc}",
            status_code=status if isinstance(status, int) else None,
        )


def _extract_id(response: Any) -> str:
    """Best-effort extraction of an entity's id from an SDK response.

    The SDK returns a pydantic-model wrapper around ``{"data": {"id": "..."}}``
    for create endpoints. We support both attribute and mapping access
    defensively so a future SDK refactor doesn't break us silently.

    Used by both ``create_customer`` and ``create_contract`` — the response
    shapes are identical at the ``data.id`` level.
    """
    # Attribute access (pydantic model): response.data.id
    data = getattr(response, "data", None)
    if data is not None:
        cid = getattr(data, "id", None) or (data.get("id") if isinstance(data, dict) else None)
        if isinstance(cid, str) and cid:
            return cid

    # Dict access: response["data"]["id"]
    if isinstance(response, dict):
        data_dict = response.get("data") or {}
        cid = data_dict.get("id") if isinstance(data_dict, dict) else None
        if isinstance(cid, str) and cid:
            return cid

    logger.error(
        "metronome_response_missing_id",
        extra={"event": "metronome_response_missing_id", "response_type": type(response).__name__},
    )
    raise PermanentMetronomeError(
        "Metronome response did not contain `data.id`."
    )


def _iter_contracts(response: Any) -> list[Any]:
    """Return the list-of-contracts inside a ``contracts.list`` response.

    SDK wraps it in a pydantic ``ContractListResponse`` with ``.data``; we
    fall back to dict access in case the SDK shape evolves.
    """
    data = getattr(response, "data", None)
    if isinstance(data, list):
        return data
    if isinstance(response, dict):
        d = response.get("data")
        if isinstance(d, list):
            return d
    return []


def _get_attr(obj: Any, name: str) -> Any:
    """Attribute-or-dict lookup. Used inside the ``contracts.list`` matcher."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)
