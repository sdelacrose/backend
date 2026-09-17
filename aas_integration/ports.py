from typing import Protocol

from .domain import AASDocument


class AASRepositoryPort(Protocol):
    def publish(self, document: AASDocument, revision_aas_id: str) -> dict: ...

    def read_environment(self, aas_id: str) -> dict: ...


class ArtifactStorePort(Protocol):
    def put_json(self, key: str, payload: bytes) -> str: ...

    def put_aasx(self, key: str, payload: bytes) -> str: ...

    def download_url(self, key: str, expires_seconds: int = 300) -> str: ...
