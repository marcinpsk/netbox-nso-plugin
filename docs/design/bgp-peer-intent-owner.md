# BGP peer intent owner

The native netbox-routing `BGPPeer` and its `BGPPeerAddressFamily` rows store
every owned BGP peer leaf. `NSOBGPPeerState` keeps identity, the `bgp_peer`
link, the lifecycle fields and `device_base_hash`. It stores no peer values.

Before this change the overlay also stored `remote_as_str` and `enabled`. The
push read those two leaves from the overlay and every other leaf from the
native peer. The read path merged four inputs (overlay intent, native content,
device content, base). Each review round on that merge found a new way to lose
operator intent or to report `in_sync` falsely.

## Rules

- One peer content function renders canonical content from a native peer. The
  push serializes it, and the read compares it with the same canonical form of
  the device entry. Normalization lives there once: ASN as uint32, AF `enabled`
  null means true, absent optional leaves. `bfd_enabled` is not on the wire and
  is not compared. The shared AF helpers used by templates do not change.
- Unowned rows keep the existing BGP three-way merge: seed, in sync, mirror,
  freeze, conflict. An existing native peer is not overwritten when the base
  is empty.
- Owned rows with a linked native peer: the read writes no native field and no
  overlay value. `matches` is canonical native content equal to device
  content. The base advances only when they match. Device drift re-pends the
  row to `accepted`, and the next Apply restores the intent.
- Owned routers follow the same drift and absence rule. `matches` is an equal
  canonical router-id and a native scope / AF membership that is a subset of
  the observed membership. Device-only scopes and AFs are not drift.
- Owned rows whose native peer is gone: the read does not recreate the native
  peer and does not settle the row. The ownership audit retracts the identity,
  as it does today, until owned native deletes are refused (#1691).
- The observation-only path never settles an owned row and writes no values.
- Peer Accept always yields `accepted`. `device_base_hash` records the last
  agreement, not the latest observation, so it cannot prove a match. A later
  qualified matching read settles the row.
- The inline edit changes native `remote_as` and `enabled`, and only the
  lifecycle fields on the overlay. Peer creation reuses a retained unowned
  overlay and refuses a retained owned one.
- Linked rows display native values. Unlinked rows display observed values from
  the `bgp` family observation snapshot, matched by router ASN, VRF and peer
  address. A missing snapshot, entry or leaf displays "unavailable".
- The migration removes the two fields and clears `device_base_hash` on every
  peer overlay, so every row re-baselines with the new hash on its next read.

Passwords compare literally. A configured password that the device read does
not report keeps the owned row `accepted`. This is conservative: it never gives
a false `in_sync`.

Native peer edits made in the netbox-routing UI do not push on their own,
because no renderer writer is active. The next read sees NetBox moved and
re-pends the row, so no intent is lost.

## Process

Two blind designs (Claude, and GPT-6.1 Sol at xhigh) agreed that the native
peer owns every leaf. The operator added that an overlay shadows its native
object: a field the native model can hold lives only there, and a missing
field is added to the netbox-routing fork. Sol proposed a retained-intent
document for deleted native peers. That proposal was rejected because #1691
refuses owned native deletes. Astra (high) ratified revision r3 in round 3.

## Not in this change

- The shared Accept status decision has the same stale-badge defect for the
  other routing families.
- OSPF owned rows mirror device changes into the native object and can report
  `in_sync` while the pushed overlay values differ.
- Switchport reads overwrite owned overlay values that the push reads.
- A netbox-routing UI persistence adapter that pushes native peer edits.
- Password comparability per NED.
