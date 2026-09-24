"""MemoryPalace: the memory service (`eventmem.core`) and its generated SDK (`eventmem.sdk`).

The earlier `.memory` file stack (store, index, recall, extract, consolidate) had no caller in
the service and was loaded by every process; it was removed in the 2026-09-24 release (E1-05).
"""

__version__ = "1.0.0"

__all__ = ["__version__"]
