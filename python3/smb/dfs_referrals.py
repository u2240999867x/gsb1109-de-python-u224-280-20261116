"""Resolve DFS referral chains for logical collection shares.

A logical share (entry) may be redirected through one or more DFS referrals
before reaching an actual storage target. Each entry carries its own validity
window and its own list of allowed targets (its authorization).

Resolution rules:
- A referral whose target matches another entry's logical path is followed,
  forming a redirect chain.
- Every hop's target is validated against the *originating* entry's allowed
  target list. Authorization is never inherited from intermediate entries:
  two entries may legitimately share the same target, but chaining through
  entry B must not grant entry A any of B's authorizations.
- Expired referrals, redirect cycles and targets outside the allowed list
  are reported separately from successfully resolved entries.

Input (JSON, via file or stdin)::

    {
      "now": "2026-11-16T00:00:00Z",            # optional, defaults to UTC now
      "referrals": [
        {
          "logical_path": "\\\\\\\\DFSROOT\\\\shareA",
          "target": "\\\\\\\\SRV1\\\\shareA",
          "expires_at": "2026-12-01T00:00:00Z",  # optional
          "allowed_targets": ["\\\\\\\\SRV1\\\\shareA"]
        }
      ]
    }

Output (JSON): resolved entries with logical path, actual target and redirect
chain, plus separate lists for cycles, out-of-bounds targets and expired
referrals.
"""

import argparse
import json
import sys
from datetime import datetime, timezone


class ReferralEntry:
    """A single DFS referral: logical path -> target, with validity and
    the entry's own allowed-target authorization."""

    def __init__(self, logical_path, target, allowed_targets, expires_at=None):
        self.logical_path = logical_path
        self.target = target
        self.allowed_targets = frozenset(allowed_targets)
        self.expires_at = expires_at

    def is_expired(self, now):
        return self.expires_at is not None and now >= self.expires_at


def _parse_timestamp(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if value.endswith('Z'):
            value = value[:-1] + '+00:00'
        parsed = datetime.fromisoformat(value)
    else:
        raise ValueError('Unsupported timestamp: %r' % (value,))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def load_referrals(data):
    """Build ReferralEntry objects from parsed JSON input."""
    entries = []
    for item in data.get('referrals', []):
        entries.append(ReferralEntry(
            logical_path=item['logical_path'],
            target=item['target'],
            allowed_targets=item.get('allowed_targets', []),
            expires_at=_parse_timestamp(item.get('expires_at')),
        ))
    return entries


def resolve_referrals(entries, now):
    """Resolve every entry's redirect chain.

    Returns a dict with keys ``resolved``, ``cycles``, ``out_of_bounds`` and
    ``expired``. ``resolved`` entries carry the logical path, the final actual
    target and the full redirect chain (logical path first, actual target
    last, intermediate targets in between).
    """
    by_path = {}
    for entry in entries:
        by_path[entry.logical_path] = entry

    report = {'resolved': [], 'cycles': [], 'out_of_bounds': [], 'expired': []}

    for entry in entries:
        # Authorization belongs to the originating entry only; it is checked
        # at every hop and never widened by intermediate entries.
        allowed = entry.allowed_targets
        chain = [entry.logical_path]
        visited = {entry.logical_path}
        current = entry

        while True:
            if current.is_expired(now):
                report['expired'].append({
                    'logical_path': entry.logical_path,
                    'chain': chain,
                    'expired_referral': current.logical_path,
                    'expires_at': current.expires_at.isoformat(),
                })
                break

            target = current.target
            chain.append(target)

            if target not in allowed:
                report['out_of_bounds'].append({
                    'logical_path': entry.logical_path,
                    'chain': chain,
                    'rejected_target': target,
                })
                break

            next_entry = by_path.get(target)
            if next_entry is None:
                report['resolved'].append({
                    'logical_path': entry.logical_path,
                    'actual_target': target,
                    'chain': chain,
                })
                break

            if target in visited:
                report['cycles'].append({
                    'logical_path': entry.logical_path,
                    'chain': chain,
                    'cycle_target': target,
                })
                break

            visited.add(target)
            current = next_entry

    return report


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Resolve DFS referral chains for logical collection shares.')
    parser.add_argument('input', nargs='?', default='-',
                        help="JSON input file with referrals, validity and allowed "
                             "target lists ('-' or omitted reads stdin)")
    parser.add_argument('--now', default=None,
                        help='Reference time (ISO 8601) for validity checks; '
                             'overrides the "now" field in the input')
    args = parser.parse_args(argv)

    raw = sys.stdin.read() if args.input == '-' else open(args.input, 'r', encoding='utf-8').read()
    data = json.loads(raw)

    now = _parse_timestamp(args.now) or _parse_timestamp(data.get('now'))
    if now is None:
        now = datetime.now(timezone.utc)

    report = resolve_referrals(load_referrals(data), now)
    json.dump(report, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
