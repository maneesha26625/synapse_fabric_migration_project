"""Extractor lookup.

The registry holds no extraction logic and no if/elif chain: extractors
declare what they support, the registry indexes those declarations by
``(artifact type, source type)``, and lookup is a dict read.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

from discovery_agent.errors import (
    DuplicateExtractorError,
    NoExtractorError,
    UnsupportedSourceError,
)
from discovery_agent.extractors.base import Extractor
from discovery_agent.extractors.models import SourceType
from discovery_agent.models import AssetType

_Key = Tuple[AssetType, SourceType]


class ExtractorRegistry:
    """Maps an artifact type and source type to the extractor that handles it."""

    def __init__(self, extractors: Iterable[Extractor] = ()) -> None:
        self._by_key: Dict[_Key, Extractor] = {}
        self._registered: List[Extractor] = []
        for extractor in extractors:
            self.register(extractor)

    def register(self, extractor: Extractor, replace: bool = False) -> None:
        """Register one extractor for every (type, source) pair it declares.

        A second extractor claiming a pair already taken raises rather than
        silently shadowing the first — which combination wins should never
        depend on import order. Pass ``replace=True`` to override deliberately.
        """
        if not extractor.supported_types:
            raise ValueError(
                f"extractor {extractor.name!r} declares no supported_types"
            )
        if not extractor.supported_sources:
            raise ValueError(
                f"extractor {extractor.name!r} declares no supported_sources"
            )

        claimed = [
            (artifact_type, source_type)
            for artifact_type in extractor.supported_types
            for source_type in extractor.supported_sources
        ]

        if not replace:
            for key in claimed:
                existing = self._by_key.get(key)
                if existing is not None:
                    raise DuplicateExtractorError(
                        f"{existing.name!r} is already registered for "
                        f"{key[0].value}/{key[1].value}; "
                        f"{extractor.name!r} cannot also claim it"
                    )

        for key in claimed:
            self._by_key[key] = extractor
        if extractor not in self._registered:
            self._registered.append(extractor)

    def find(
        self, artifact_type: AssetType, source_type: SourceType
    ) -> Optional[Extractor]:
        """The extractor for this pair, or None."""
        return self._by_key.get((artifact_type, source_type))

    def require(self, artifact_type: AssetType, source_type: SourceType) -> Extractor:
        """The extractor for this pair, or a typed error explaining which gap it is.

        An artifact type nobody handles and an artifact type handled only for a
        different source are different problems, so they raise different errors.
        """
        extractor = self.find(artifact_type, source_type)
        if extractor is not None:
            return extractor
        if artifact_type in self.supported_types():
            raise UnsupportedSourceError(
                f"no extractor reads {artifact_type.value} from "
                f"source {source_type.value}"
            )
        raise NoExtractorError(f"no extractor registered for {artifact_type.value}")

    def supports(self, artifact_type: AssetType, source_type: SourceType) -> bool:
        return (artifact_type, source_type) in self._by_key

    def supported_types(self) -> Tuple[AssetType, ...]:
        """Every artifact type some extractor handles, for any source."""
        return tuple(sorted({key[0] for key in self._by_key}, key=lambda t: t.value))

    def supported_sources(self, artifact_type: AssetType) -> Tuple[SourceType, ...]:
        return tuple(
            sorted(
                {key[1] for key in self._by_key if key[0] is artifact_type},
                key=lambda s: s.value,
            )
        )

    @property
    def extractors(self) -> Tuple[Extractor, ...]:
        """Registered extractors, in registration order."""
        return tuple(self._registered)

    def __len__(self) -> int:
        return len(self._registered)

    def __contains__(self, key: object) -> bool:
        return key in self._by_key
