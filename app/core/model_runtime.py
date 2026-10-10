"""Consistent local inference device selection; hosted inference does not use this."""

from app.core.config import settings


def local_model_device():
    if settings.local_model_device == "cpu":
        return "cpu"
    import torch
    available = torch.backends.mps.is_available()
    if settings.local_model_device == "mps" and not available:
        raise RuntimeError("LOCAL_MODEL_DEVICE=mps requires an available Apple Metal backend.")
    return "mps" if available else "cpu"
