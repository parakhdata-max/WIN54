"""
WIN54 application package.

Keep this top-level package init intentionally light. Subpackages such as
``modules.loaders`` expose their own public surfaces; importing ``modules``
should not bootstrap loader/database code as a side effect.
"""

__all__ = []
