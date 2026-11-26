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
from typing import Final, TypeAlias

from .frame import BROADCAST, Telegram, is_slave

NO_ACK: Final = "no_ack"  # the receiver did not acknowledge at all
REFUSED: Final = "refused"  # NAKed, and the resend NAKed too (or none followed)
REPEATED: Final = "repeated"  # NAKed once, taken on the resend
ANSWER_REFUSED: Final = "answer_refused"  # the master NAKed the receiver's answer
ACK_FAULTS: Final = (NO_ACK, REFUSED, REPEATED, ANSWER_REFUSED)
# the read arrived (acknowledged), but its answer is missing, or empty although
# the read expects data (e.g. a controller too busy to answer its gateway)
UNANSWERED: Final = "unanswered"
EMPTY_ANSWER: Final = "empty_answer"
FAULTS: Final = (*ACK_FAULTS, UNANSWERED, EMPTY_ANSWER)
# a command: sender, receiver, command bytes, message name ("" when unknown)
CommandKey: TypeAlias = tuple[int, int, str, str]
LOST: Final = frozenset((NO_ACK, REFUSED))  # the command did not arrive


def fault_of(tg: Telegram) -> str | None:
    """What went wrong with a telegram's acknowledgments or its answer; None
    when nothing did (or for a broadcast, which nobody acknowledges). An empty
    answer needs the message definition: the engine tells it."""
    if tg.zz == BROADCAST:
        return None
    if tg.master_ack is None:
        return NO_ACK
    if tg.master_ack == 0xFF:
        return REFUSED
    if is_slave(tg.zz) and tg.slave_crc is None:
        return UNANSWERED  # a device always answers a master's telegram
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
    # how often in a row this command (sender, receiver, message) was lost, this
    # one included; 0 when it arrived. A single loss is bus noise the sender
    # repeats; a command lost again and again does not get through
    streak: int = 0

    @property
    def ack(self) -> bool:
        """An acknowledgment failed (else the answer is missing or empty)."""
        return self.fault in ACK_FAULTS

    @property
    def lost(self) -> bool:
        """The command did not arrive (not acknowledged, or refused for good)."""
        return self.fault in LOST

    def as_dict(self) -> dict[str, object]:
        return {"at": self.at, "qq": f"{self.qq:02x}", "zz": f"{self.zz:02x}",
                "pbsb": self.pbsb, "data": self.data.hex(), "fault": self.fault,
                "message": self.message, "lost": self.lost, "own": self.own,
                "streak": self.streak}
