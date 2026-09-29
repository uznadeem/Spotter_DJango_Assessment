from dataclasses import dataclass

import requests
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


MAX_BATCH_SIZE = 1000
MIN_PRIORITY = 0.5
MAX_PRIORITY = 1.0


class GeoapifyError(Exception):
    """Base exception for Geoapify integration failures."""


class GeoapifyRateLimitError(GeoapifyError):
    """Raised when Geoapify rejects a request due to its quota."""


class GeoapifyResponseError(GeoapifyError):
    """Raised when Geoapify returns an invalid response."""


@dataclass(frozen=True)
class GeoapifyBatchResult:
    job_id: str
    is_pending: bool
    results: tuple[dict, ...] = ()


class GeoapifyClient:
    def __init__(
        self,
        *,
        api_key=None,
        session=None,
    ):
        self.api_key = (
            api_key
            if api_key is not None
            else settings.GEOAPIFY_API_KEY
        )

        if not self.api_key:
            raise ImproperlyConfigured(
                "GEOAPIFY_API_KEY is not configured."
            )

        self.base_url = settings.GEOAPIFY_BASE_URL.rstrip("/")
        self.timeout = settings.HTTP_TIMEOUT_SECONDS
        self.session = session or requests.Session()

    def submit_geocoding_batch(
        self,
        inputs,
        *,
        priority=0.5,
    ):
        normalized_inputs = tuple(inputs)

        if not normalized_inputs:
            raise ValueError(
                "At least one geocoding input is required."
            )

        if len(normalized_inputs) > MAX_BATCH_SIZE:
            raise ValueError(
                f"A Geoapify batch cannot exceed "
                f"{MAX_BATCH_SIZE} inputs."
            )

        if not MIN_PRIORITY <= priority <= MAX_PRIORITY:
            raise ValueError(
                "Geoapify priority must be between "
                f"{MIN_PRIORITY} and {MAX_PRIORITY}."
            )

        request_inputs = []

        for item in normalized_inputs:
            input_id = str(item["id"]).strip()
            text = str(item["text"]).strip()

            if not input_id:
                raise ValueError(
                    "Every Geoapify input requires an ID."
                )

            if not text:
                raise ValueError(
                    "Every Geoapify input requires text."
                )

            request_inputs.append(
                {
                    "id": input_id,
                    "params": {
                        "text": text,
                    },
                }
            )

        payload = {
            "api": "/v1/geocode/search",
            "params": {
                "filter": "countrycode:us",
                "format": "json",
                "lang": "en",
                "limit": 1,
            },
            "priority": priority,
            "inputs": request_inputs,
        }

        response = self._request(
            method="post",
            json=payload,
        )

        return self._parse_response(response)

    def get_batch_result(self, job_id):
        normalized_job_id = str(job_id).strip()

        if not normalized_job_id:
            raise ValueError(
                "A Geoapify job ID is required."
            )

        response = self._request(
            method="get",
            extra_params={
                "id": normalized_job_id,
            },
        )

        return self._parse_response(
            response,
            fallback_job_id=normalized_job_id,
        )

    def _request(
        self,
        *,
        method,
        json=None,
        extra_params=None,
    ):
        params = {
            "apiKey": self.api_key,
        }
        params.update(extra_params or {})

        try:
            response = self.session.request(
                method=method,
                url=f"{self.base_url}/v1/batch",
                params=params,
                json=json,
                timeout=self.timeout,
            )
        except requests.RequestException as error:
            raise GeoapifyError(
                "Could not connect to Geoapify."
            ) from error

        if response.status_code == 429:
            raise GeoapifyRateLimitError(
                "Geoapify rate limit or daily quota reached."
            )

        if response.status_code not in {200, 202}:
            raise GeoapifyError(
                "Geoapify request failed with HTTP status "
                f"{response.status_code}."
            )

        return response

    @staticmethod
    def _parse_response(
        response,
        *,
        fallback_job_id=None,
    ):
        try:
            payload = response.json()
        except ValueError as error:
            raise GeoapifyResponseError(
                "Geoapify returned invalid JSON."
            ) from error

        if not isinstance(payload, dict):
            raise GeoapifyResponseError(
                "Geoapify returned an invalid batch response."
            )

        job_id = payload.get("id") or fallback_job_id

        if not isinstance(job_id, str) or not job_id:
            raise GeoapifyResponseError(
                "Geoapify response is missing the job ID."
            )

        if response.status_code == 202:
            return GeoapifyBatchResult(
                job_id=job_id,
                is_pending=True,
            )

        results = payload.get("results")

        if not isinstance(results, list):
            raise GeoapifyResponseError(
                "Completed Geoapify response is missing results."
            )

        if not all(
            isinstance(result, dict)
            for result in results
        ):
            raise GeoapifyResponseError(
                "Geoapify returned invalid result entries."
            )

        return GeoapifyBatchResult(
            job_id=job_id,
            is_pending=False,
            results=tuple(results),
        )
