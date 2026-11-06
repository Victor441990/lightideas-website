"""Server-side client for the private Biztrack landing-page integration API."""

from __future__ import annotations

from urllib.parse import urljoin

import requests
from flask import current_app


class BiztrackServiceError(RuntimeError):
    pass


def _request(path: str, params: dict[str, str] | None = None) -> requests.Response:
    base_url = current_app.config.get('BIZTRACK_SERVICE_URL')
    token = current_app.config.get('BIZTRACK_INTEGRATION_TOKEN')
    if not base_url or not token:
        raise BiztrackServiceError('Biztrack integration is not configured.')
    try:
        response = requests.get(
            urljoin(f'{base_url.rstrip("/")}/', path.lstrip('/')),
            params=params,
            headers={'Authorization': f'Bearer {token}'},
            timeout=(2, 8),
        )
    except requests.RequestException as error:
        raise BiztrackServiceError('Biztrack service is unavailable.') from error
    if response.status_code != 200:
        raise BiztrackServiceError('Biztrack service rejected the request.')
    return response


def overview() -> dict:
    try:
        return _request('/api/integrations/lightideas/overview').json()
    except ValueError as error:
        raise BiztrackServiceError('Biztrack service returned an invalid response.') from error


def registrations(filters: dict[str, str]) -> dict:
    params = {key: value for key, value in filters.items() if value}
    try:
        return _request('/api/integrations/lightideas/registrations', params).json()
    except ValueError as error:
        raise BiztrackServiceError('Biztrack service returned an invalid response.') from error


def export_csv(filters: dict[str, str]) -> bytes:
    params = {key: value for key, value in filters.items() if value}
    return _request('/api/integrations/lightideas/export.csv', params).content
