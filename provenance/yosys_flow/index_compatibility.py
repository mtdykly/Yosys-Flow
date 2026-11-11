"""Explicit reviewed reader upgrades; authentication is still mandatory.

Each entry binds the COMPLETE old and new implementation fingerprints. It is
not a schema-only bypass: any subsequent code change needs a fresh review.
This policy file is outside that fingerprint to avoid a self-referential hash.
"""
import hashlib
import json


REVIEWED_READER_UPGRADES = frozenset({
    # schema 3 before shared-array interning -> semantics-preserving reader;
    # the archived signature and all raw/database hashes remain mandatory.
    ('1d595edcbde4f58ce19367d7f0095cad7db7f834e0f12f6f67eb2e1600ffefe5',
     '16bb3f2c362554091a386a1b9950b36b6558bdb9e9daa7c12b0f6365ff0c7811'),
})


def fingerprint_digest(implementation):
    return hashlib.sha256(json.dumps(implementation,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def compatible(stored, current):
    return stored == current or (fingerprint_digest(stored),fingerprint_digest(current)) in REVIEWED_READER_UPGRADES
