"""
Delivery path analysis for archived email messages.

Explains how a message was delivered using three separately stored time sources:
- the Date header (sender-claimed, never trusted),
- the Received header chain (trusted only inside the caller-defined relay boundary),
- the server internal date (IMAP INTERNALDATE, trusted when provided by the caller).

Header order is the path constraint: Received headers are prepended by each relay,
so headers[0] is the last hop. The path is never re-sorted by time values.
"""
import datetime
import re
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Callable, List, Optional, Sequence, Tuple, Union

__all__ = [
    'ReceivedHop', 'HopDelay', 'HeaderConflict', 'DeliveryReport',
    'analyze_delivery', 'analyze_message',
]

DEFAULT_MAX_CLOCK_SKEW = datetime.timedelta(minutes=5)

_COMMENT_RE = re.compile(r'\([^)]*\)')
_FROM_RE = re.compile(r'\bfrom\s+([^\s;]+)', re.IGNORECASE)
_BY_RE = re.compile(r'\bby\s+([^\s;]+)', re.IGNORECASE)

TrustedMatcher = Union[Sequence[str], Callable[[str], bool]]


@dataclass
class ReceivedHop:
    """One Received header, in header order (index 0 = last hop, closest to destination)."""
    index: int
    raw: str
    from_host: Optional[str]
    by_host: Optional[str]
    timestamp: Optional[datetime.datetime]  # None when the date-time part is missing/unparsable
    trusted: bool


@dataclass
class HopDelay:
    """
    Delay range between two path-consecutive hops (the message moved earlier_hop -> later_hop).
    min_delay/max_delay are None when either hop lacks a trusted timestamp - never filled with zero.
    order_certain is False when the ranges overlap within the allowed clock skew.
    """
    earlier_hop: int
    later_hop: int
    min_delay: Optional[datetime.timedelta]
    max_delay: Optional[datetime.timedelta]
    order_certain: bool


@dataclass
class HeaderConflict:
    """Two time sources that contradict each other beyond the allowed clock skew."""
    first: str
    second: str
    reason: str


@dataclass
class DeliveryReport:
    """Result of delivery path analysis. All time sources are stored separately."""
    date_header: Optional[datetime.datetime]  # sender-claimed, untrusted
    internal_date: Optional[datetime.datetime]  # server internal time
    hops: List[ReceivedHop]
    delivery_window: Optional[Tuple[datetime.datetime, datetime.datetime]]  # provable interval
    hop_delays: List[HopDelay]
    conflicts: List[HeaderConflict] = field(default_factory=list)


def _parse_received_date(raw: str) -> Optional[datetime.datetime]:
    """Date-time is the part after the last ';' in a Received header."""
    if ';' not in raw:
        return None
    date_part = _COMMENT_RE.sub('', raw.rsplit(';', 1)[1]).strip()
    if not date_part:
        return None
    try:
        return parsedate_to_datetime(date_part)
    except (TypeError, ValueError):
        return None


def _as_aware(dt: datetime.datetime) -> datetime.datetime:
    """Naive datetimes are assumed to be UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def _host_matcher(trusted_relays: TrustedMatcher) -> Callable[[str], bool]:
    if callable(trusted_relays):
        return trusted_relays
    exact = {h.lower() for h in trusted_relays if not h.startswith('.')}
    suffixes = tuple(h.lower() for h in trusted_relays if h.startswith('.'))

    def match(host: str) -> bool:
        host_lower = host.lower()
        return host_lower in exact or any(host_lower.endswith(s) for s in suffixes)

    return match


def _parse_hop(index: int, raw: str, trusted: bool) -> ReceivedHop:
    from_match = _FROM_RE.search(raw)
    by_match = _BY_RE.search(raw)
    timestamp = _parse_received_date(raw)
    return ReceivedHop(
        index=index,
        raw=raw,
        from_host=from_match.group(1) if from_match else None,
        by_host=by_match.group(1) if by_match else None,
        timestamp=_as_aware(timestamp) if timestamp else None,
        trusted=trusted,
    )


def analyze_delivery(
        received_headers: Sequence[str],
        date_header: Optional[str] = None,
        internal_date: Optional[datetime.datetime] = None,
        trusted_relays: TrustedMatcher = (),
        max_clock_skew: datetime.timedelta = DEFAULT_MAX_CLOCK_SKEW,
) -> DeliveryReport:
    """
    Analyze the delivery path of an archived message.

    :param received_headers: Received header values in header order (headers[0] = last hop).
    :param date_header: raw Date header value (sender-claimed, stored but never trusted).
    :param internal_date: server internal time (IMAP INTERNALDATE), trusted when given.
    :param trusted_relays: trusted relay boundary - hostnames ('mx.example.com'),
        domain suffixes ('.example.com') or a callable(host) -> bool.
        A hop is trusted only while its 'by' host matches, walking from the top;
        the first non-matching hop ends the trusted boundary and nothing below it
        (possibly forged by the sender side) is promoted to a trusted observation.
    :param max_clock_skew: allowed clock difference between any two servers.
    :return: DeliveryReport with the provable delivery window, per-hop delay ranges
        and mutually conflicting headers.
    """
    if max_clock_skew < datetime.timedelta(0):
        raise ValueError('max_clock_skew must not be negative')
    is_trusted_host = _host_matcher(trusted_relays)

    hops = []
    inside_boundary = True
    for index, raw in enumerate(received_headers):
        by_match = _BY_RE.search(raw)
        by_host = by_match.group(1) if by_match else None
        if inside_boundary and not (by_host and is_trusted_host(by_host)):
            inside_boundary = False
        hops.append(_parse_hop(index, raw, trusted=inside_boundary))

    date_dt = None
    if date_header:
        try:
            date_dt = _as_aware(parsedate_to_datetime(date_header))
        except (TypeError, ValueError):
            date_dt = None
    internal_dt = _as_aware(internal_date) if internal_date else None

    # provable delivery window: only trusted observations (trusted hops + internal date)
    trusted_times = [h.timestamp for h in hops if h.trusted and h.timestamp]
    if internal_dt:
        trusted_times.append(internal_dt)
    delivery_window = None
    if trusted_times:
        delivery_window = (min(trusted_times) - max_clock_skew, max(trusted_times) + max_clock_skew)

    # per-hop delay ranges along the path (never re-sorted by time)
    hop_delays = []
    conflicts = []
    for later in hops:  # message moved hops[i+1] -> hops[i]
        earlier = hops[later.index + 1] if later.index + 1 < len(hops) else None
        if earlier is None:
            break
        if not (earlier.trusted and later.trusted and earlier.timestamp and later.timestamp):
            hop_delays.append(HopDelay(earlier.index, later.index, None, None, False))
            continue
        diff = later.timestamp - earlier.timestamp
        uncertainty = 2 * max_clock_skew
        min_delay, max_delay = diff - uncertainty, diff + uncertainty
        hop_delays.append(HopDelay(
            earlier.index, later.index, min_delay, max_delay, order_certain=min_delay > datetime.timedelta(0)))
        if max_delay < datetime.timedelta(0):
            conflicts.append(HeaderConflict(
                first='Received[{}]'.format(earlier.index),
                second='Received[{}]'.format(later.index),
                reason='path order contradicts timestamps beyond allowed clock skew',
            ))

    # Date header vs deepest trusted hop: message cannot be sent after it was received
    trusted_hops = [h for h in hops if h.trusted and h.timestamp]
    if date_dt and trusted_hops:
        boundary_hop = trusted_hops[-1]
        if date_dt > boundary_hop.timestamp + max_clock_skew:
            conflicts.append(HeaderConflict(
                first='Date',
                second='Received[{}]'.format(boundary_hop.index),
                reason='sender-claimed Date is later than trusted receipt beyond allowed clock skew',
            ))

    # internal date vs final trusted hop: server cannot store the message before receiving it
    if internal_dt and trusted_hops:
        final_hop = trusted_hops[0]
        if internal_dt < final_hop.timestamp - 2 * max_clock_skew:
            conflicts.append(HeaderConflict(
                first='Received[{}]'.format(final_hop.index),
                second='INTERNALDATE',
                reason='server internal time precedes final trusted hop beyond allowed clock skew',
            ))

    return DeliveryReport(
        date_header=date_dt,
        internal_date=internal_dt,
        hops=hops,
        delivery_window=delivery_window,
        hop_delays=hop_delays,
        conflicts=conflicts,
    )


def analyze_message(
        message,
        internal_date: Optional[datetime.datetime] = None,
        trusted_relays: TrustedMatcher = (),
        max_clock_skew: datetime.timedelta = DEFAULT_MAX_CLOCK_SKEW,
) -> DeliveryReport:
    """Analyze delivery of a MailMessage (or any object with .obj and .date_str)."""
    received = message.obj.get_all('Received', [])
    return analyze_delivery(
        received_headers=received,
        date_header=message.date_str or None,
        internal_date=internal_date,
        trusted_relays=trusted_relays,
        max_clock_skew=max_clock_skew,
    )
