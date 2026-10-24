"""Pre-fix Mind.manage_desire from 12da5dccf85648685a81107a078ecae222afb1f5.

Only this method is retained, so the old/new regression works in a shallow
public checkout without access to the private release history.
"""
from kin_mind.state import DesireChange


class Mind:
    def manage_desire(self, request: DesireChange):
        return self._mutate(
            request,
            "desire",
            lambda conn, state, eid: self._apply_desire(conn, state, request, eid),
        )
