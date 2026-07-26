"""Tool panels package — one widget per tool.

Each tool is a QWidget subclass that:
  * Provides its own options UI
  * Knows how to validate the options
  * Has a ``run(parent_window)`` coroutine that performs the work and
    reports progress through the standard Worker callbacks.
"""
from .base import BaseTool

__all__ = ["BaseTool"]
