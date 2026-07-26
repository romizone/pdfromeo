"""Background workers (QThread wrappers) so the UI stays responsive."""
from .background import Worker

__all__ = ["Worker"]
