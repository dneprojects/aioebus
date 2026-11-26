"""Acknowledgments of the telegrams on the bus: other masters' read passively,
our own from the transaction that sent them.

Every telegram that is no broadcast is acknowledged by its receiver: ACK (0x00)
when it took the master part, NAK (0xFF) when it arrived garbled (the master then
sends it once more). A master acknowledges a slave's answer the same way. A
missing or refused acknowledgment shows a command that did not get through --
e.g. a controller's relay command the mixer module never took -- without any
traffic of ours.

Pure: ``fault_of`` reads a parsed telegram (an active driver has ``ack_fault``),
``AckFault`` is the record of one.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from .frame import BROADCAST, Telegram

NO_ACK: Final = "no_ack"  # the receiver did not acknowledge at all
REFUSED: Final = "refused"  # NAKed, and the resend NAKed too (or none followed)
REPEATED: Final = "repeated"  # NAKed once, taken on the resend
ANSWER_REFUSED: Final = "answer_refused"  # the master NAKed the receiver's answer
FAULTS: Final = (NO_ACK, REFUSED, REPEATED, ANSWER_REFUSED)
LOST: Final = frozenset((NO_ACK, REFUSED))  # the command did not arrive


def fault_of(tg: Telegram) -> str | None:
    """What went wrong with a telegram's acknowledgments; None when nothing did
    (or for a broadcast, which nobody acknowledges)."""
    if tg.zz == BROADCAST:
        return None
    if tg.master_ack is None:
        return NO_ACK
    if tg.master_ack == 0xFF:
        return REFUSED
    if tg.master_naks:
        return REPEATED
    if tg.slave_naks:
        return ANSWER_REFUSED
    return None


@dataclass(frozen=True)
class AckFault:
    """One telegram whose acknowledgment failed: when (wall clock), who sent it
    to whom, the command and its data, and what went wrong."""

    at: float  # time.time() of the telegram
    qq: int
    zz: int
    pbsb: str
    data: bytes  # the master data (message id first)
    fault: str  # one of FAULTS
    message: str | None = None  # the decoded message name, when known
    own: bool = False  # one of our telegrams (else another master's)

    @property
    def lost(self) -> bool:
        """The command did not arrive (not acknowledged, or refused for good)."""
        return self.fault in LOST

    def as_dict(self) -> dict[str, object]:
        return {"at": self.at, "qq": f"{self.qq:02x}", "zz": f"{self.zz:02x}",
                "pbsb": self.pbsb, "data": self.data.hex(), "fault": self.fault,
                "message": self.message, "lost": self.lost, "own": self.own}
