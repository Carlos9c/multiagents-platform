from __future__ import annotations

from abc import ABC, abstractmethod

from app.services.image_providers.contracts import ImageGenerationRequest, ImageGenerationResult


class BaseImageProvider(ABC):
    @abstractmethod
    def generate(self, request: ImageGenerationRequest) -> ImageGenerationResult:
        """
        Generate an image from the request and return the raw bytes plus metadata.

        Implementations should raise on API or transport failures.
        """
        raise NotImplementedError
