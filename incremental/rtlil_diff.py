"""Bounded, order-preserving RTLIL object comparison.

Object shapes propose a module-wide bijection of generated identifiers. They
are never equality evidence by themselves: all tokens are compared after that
single renaming. Ambiguous matches and exhausted work budgets become edits.
"""
from bisect import bisect_left
from dataclasses import dataclass
import time

# Exact LCS is restricted to small gaps, with one budget shared by all objects
# of a module. Large gaps use unique anchors or a conservative replacement.
MAX_GAP_SIDE = 256
MAX_GAP_CELLS = 16_384
MAX_MODULE_CELLS = 262_144
ALIGNMENT_PASSES = 3
MAX_POSITIONAL_EDITS = 256


@dataclass
class Work:
    remaining: int = MAX_MODULE_CELLS
    cells: int = 0
    fallbacks: int = 0


def _unique(values, start=0):
    positions = {}
    for i, value in enumerate(values, start):
        positions[value] = i if value not in positions else None
    return {key: i for key, i in positions.items() if i is not None}


def _anchors(a, b, a0, a1, b0, b1):
    """Unique exact keys, restricted to an increasing sequence in both sides."""
    left, right = _unique(a[a0:a1], a0), _unique(b[b0:b1], b0)
    pairs = [(i, right[key]) for key, i in left.items() if key in right]
    tails, ends, previous = [], [], []
    for index, (_, j) in enumerate(pairs):
        place = bisect_left(tails, j)
        previous.append(ends[place-1] if place else -1)
        if place == len(tails):
            tails.append(j); ends.append(index)
        else:
            tails[place] = j; ends[place] = index
    result = []
    index = ends[-1] if ends else -1
    while index != -1:
        result.append(pairs[index]); index = previous[index]
    return result[::-1]


def _small_matches(a, b, a0, a1, b0, b1, work):
    n, m = a1-a0, b1-b0
    if not n or not m:
        return []
    cost = n*m
    if max(n, m) > MAX_GAP_SIDE or cost > min(MAX_GAP_CELLS, work.remaining):
        work.fallbacks += 1
        return []
    work.remaining -= cost; work.cells += cost
    lengths = [[0]*(m+1) for _ in range(n+1)]
    for i in range(n-1, -1, -1):
        row, below = lengths[i], lengths[i+1]
        for j in range(m-1, -1, -1):
            row[j] = below[j+1]+1 if a[a0+i] == b[b0+j] else max(below[j], row[j+1])
    result = []; i = j = 0
    while i < n and j < m:
        if a[a0+i] == b[b0+j]:
            result.append((a0+i, b0+j)); i += 1; j += 1
        elif lengths[i+1][j] >= lengths[i][j+1]:
            i += 1
        else:
            j += 1
    return result


def opcodes(a, b, work):
    """Linear scans + O(N log N) anchors + globally bounded exact gap work.

    No recursive search, popular-row suppression, or unbounded repeated-token
    matching. Every equal opcode contains exactly equal keys. A failed match
    may enlarge an edit, but cannot hide one.
    """
    start = 0
    while start < min(len(a), len(b)) and a[start] == b[start]:
        start += 1
    a1, b1 = len(a), len(b)
    while a1 > start and b1 > start and a[a1-1] == b[b1-1]:
        a1 -= 1; b1 -= 1
    matches = [(0, 0, start)] if start else []
    i = j = start
    for x, y in [*_anchors(a, b, start, a1, start, b1), (a1, b1)]:
        matches.extend((p, q, 1) for p, q in _small_matches(a, b, i, x, j, y, work))
        if x < a1 and y < b1:
            matches.append((x, y, 1))
        i, j = x+1, y+1
    if a1 < len(a):
        matches.append((a1, b1, len(a)-a1))
    # Merge adjacent matches to avoid one opcode per identical token row.
    runs = []
    for i, j, length in matches:
        if runs and runs[-1][0]+runs[-1][2] == i and runs[-1][1]+runs[-1][2] == j:
            x, y, size = runs[-1]; runs[-1] = x, y, size+length
        else:
            runs.append((i, j, length))
    x = y = 0
    for i, j, size in [*runs, (len(a), len(b), 0)]:
        if x < i or y < j:
            yield ('replace' if x < i and y < j else 'delete' if x < i else 'insert'), x, i, y, j
        if size:
            yield 'equal', i, i+size, j, j+size
        x, y = i+size, j+size


def row_key(row):
    return tuple(row) if isinstance(row, list) else row


def generated(token):
    return token.startswith('@generated_') and token[11:].isdigit()


def _positional_edits(old, new):
    """Exact identity-map comparison for sparse, name-stable edits.

    Equal rows are compared in full. Every changed row must retain the same
    sequence of generated references, so one identity mapping covers the whole
    module. Never infer equality from cell shapes, names, or a digest. Numbering
    shifts, insertions/deletions and dense edits use the bounded general matcher.
    """
    if len(old) != len(new):
        return None
    edits = []; changed = 0
    for i, (a, b) in enumerate(zip(old, new)):
        if a == b:
            continue
        changed += 1
        if changed > MAX_POSITIONAL_EDITS:
            return None
        if [t for t in a if generated(t)] != [t for t in b if generated(t)]:
            return None
        if edits and edits[-1][2] == i:
            tag, start, _, other, _ = edits[-1]
            edits[-1] = tag, start, i+1, other, i+1
        else:
            edits.append(('replace', i, i+1, i, i+1))
    return edits


def _shape(rows, known):
    local = {}
    return tuple(tuple(('matched', known[t]) if t in known else
                       ('local', local.setdefault(t, len(local))) if generated(t) else ('literal', t)
                       for t in row) for row in rows)


def _head(rows):
    return next((row for row in rows if row[0] != 'attribute'), ())


def _block_key(rows):
    head = _head(rows)
    if head and head[0] in {'wire', 'memory', 'cell', 'process'} and head[-1].startswith('\\'):
        return 'named', head[0], head[-1]
    if len(head) > 1 and head[0] == 'connect' and head[1].startswith('\\'):
        return 'connection', head[1]
    return 'body', tuple(map(tuple, rows))


def _identity(rows, known):
    """A pin/target anchor proposes the declaration name ONLY.

    This also aligns changed cells; their type, parameters, port values and
    process priority are still compared in full, never normalized away.
    """
    head = _head(rows)
    if not head or head[0] not in {'cell', 'process'} or not generated(head[-1]):
        return None
    pins = set()
    for row in rows:
        if head[0] == 'cell' and row[0] == 'connect':
            for token in row[2:]:
                if token in known or token.startswith('\\'):
                    pins.add((row[1], known.get(token, token)))
        elif head[0] == 'process' and row[0] in {'assign', 'update'}:
            token = row[1]
            if token in known or token.startswith('\\'):
                pins.add((row[0], known.get(token, token)))
    return (head[0], tuple(sorted(pins))) if pins else None


def _alignment(old, new):
    forward, reverse = {}, {}
    # Separate object namespaces even when two declarations have identical
    # local spelling. All uses of a temporary share the same global binding.
    def roles(blocks):
        result = {}
        for rows in blocks:
            head = _head(rows)
            if head and head[0] in {'wire', 'cell', 'process'} and generated(head[-1]):
                result[head[-1]] = head[0]
        return result
    old_roles, new_roles = roles(old), roles(new)
    conflicts = passes = 0
    for _ in range(ALIGNMENT_PASSES):
        passes += 1
        old_known = {t:t for t in reverse}
        left = _unique([_shape(rows, old_known) for rows in old])
        right = _unique([_shape(rows, forward) for rows in new])
        candidates = [(i, right[key]) for key, i in left.items() if key in right]
        # Public connection targets give especially useful identity anchors.
        candidates.sort(key=lambda pair: _head(old[pair[0]])[0] != 'connect')
        before = len(forward)
        for i, j in candidates:
            proposed, inverse = {}, {}
            valid = True
            for a, b in zip(old[i], new[j]):
                for x, y in zip(a, b):
                    if not generated(x):
                        continue
                    if (not generated(y) or old_roles.get(x) != new_roles.get(y) or
                            (y in forward and forward[y] != x) or (x in reverse and reverse[x] != y) or
                            (y in proposed and proposed[y] != x) or (x in inverse and inverse[x] != y)):
                        valid = False; break
                    proposed[y] = x; inverse[x] = y
                if not valid:
                    break
            if valid:
                forward.update(proposed); reverse.update(inverse)
            else:
                conflicts += 1
        old_known = {t:t for t in reverse}
        left = _unique([_identity(rows, old_known) for rows in old])
        right = _unique([_identity(rows, forward) for rows in new])
        for key, i in left.items():
            if key is None or key not in right:
                continue
            x, y = _head(old[i])[-1], _head(new[right[key]])[-1]
            if (y in forward and forward[y] != x) or (x in reverse and reverse[x] != y):
                conflicts += 1
            else:
                forward[y] = x; reverse[x] = y
        if len(forward) == before:
            break
    return forward, conflicts, passes


def compare_objects(old, new, old_spans, new_spans):
    """Return diagnostic edits; equality always checks the full renamed body.

    Snippets retain the input canonical identifiers. Omitted name-only changes
    are covered by a single injective mapping, not independent per-cell names.
    """
    started = time.perf_counter()
    edits = _positional_edits(old, new)
    timings = {'positional_scan_seconds': time.perf_counter()-started}
    stats = {'method':'structured_bounded_rtlil_v2', 'old_rows':len(old), 'new_rows':len(new),
             'old_objects':len(old_spans), 'new_objects':len(new_spans),
             'positional_edit_limit':MAX_POSITIONAL_EDITS, 'timings_seconds':timings,
             'functional_equivalence_claimed':False}
    if edits is not None:
        stats.update(matching_path='exact_identity', aligned_generated_identifiers=0,
                     alignment_conflicts=0, alignment_passes=0, exact_gap_work_cells=0,
                     exact_gap_work_limit=MAX_MODULE_CELLS, conservative_gap_fallbacks=0,
                     name_alignment_scope='module_wide_identity')
        return edits, stats
    step = time.perf_counter()
    old_blocks = [old[i:j] for i, j in old_spans]
    new_blocks = [new[i:j] for i, j in new_spans]
    timings['object_materialization_seconds'] = time.perf_counter()-step
    step = time.perf_counter()
    mapping, conflicts, passes = _alignment(old_blocks, new_blocks)
    timings['identifier_alignment_seconds'] = time.perf_counter()-step
    step = time.perf_counter()
    renamed = [[mapping.get(t, '@unmatched_new_'+t if generated(t) else t) for t in row] for row in new]
    renamed_blocks = [renamed[i:j] for i, j in new_spans]
    timings['rename_materialization_seconds'] = time.perf_counter()-step
    step = time.perf_counter()
    work = Work(); edits = []
    def position(spans, i, total):
        return spans[i][0] if i < len(spans) else total
    for tag, i, j, k, l in opcodes(list(map(_block_key, old_blocks)), list(map(_block_key, renamed_blocks)), work):
        if tag == 'equal':
            for a, b in zip(range(i, j), range(k, l)):
                if old_blocks[a] == renamed_blocks[b]:
                    continue
                x, y = old_spans[a][0], new_spans[b][0]
                for kind, u, v, w, z in opcodes(list(map(row_key, old_blocks[a])), list(map(row_key, renamed_blocks[b])), work):
                    if kind != 'equal':
                        edits.append((kind, x+u, x+v, y+w, y+z))
        else:
            x, y = position(old_spans, i, len(old)), position(new_spans, k, len(new))
            xe, ye = position(old_spans, j, len(old)), position(new_spans, l, len(new))
            # Small changed cells still get useful parameter/port-level edits.
            for kind, u, v, w, z in opcodes(list(map(row_key, old[x:xe])), list(map(row_key, renamed[y:ye])), work):
                if kind != 'equal':
                    edits.append((kind, x+u, x+v, y+w, y+z))
    timings['bounded_comparison_seconds'] = time.perf_counter()-step
    stats.update(matching_path='bounded_alignment',
        aligned_generated_identifiers=len(mapping), alignment_conflicts=conflicts,
        alignment_passes=passes, exact_gap_work_cells=work.cells,
        exact_gap_work_limit=MAX_MODULE_CELLS, conservative_gap_fallbacks=work.fallbacks,
        name_alignment_scope='module_wide_bijection')
    return edits, stats
