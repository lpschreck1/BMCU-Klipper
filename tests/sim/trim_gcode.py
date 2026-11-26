"""Shrink a real Orca G-code export for the simulator.

    python tests/sim/trim_gcode.py IN.gcode OUT.gcode [--every N] [--edge K] [--context C]

What BMCU and Snapmaker react to is kept exactly: the header and config blocks, the start
and end G-code, every command that is not a plain move (tool changes, ``BMCU_*``,
``SET_PRINT_*``, temperatures, ``G92``, ``M82/M83``...), the layer markers, and
``--context`` (default 30) lines around each of the important ones (tool changes, BMCU and
print-info commands, temperatures, layer changes). The moves between them are thinned: the
first and last ``--edge`` (default 6) moves of each layer are kept and every ``--every``
(default 12) th move in between.

Extrusion is preserved: with relative extrusion (``M83``) the E of every dropped move is added
to the next kept move (and flushed with one extra move at the end of the layer), so each
layer's net E distance is the same as the original; with absolute extrusion (``M82``) the
kept moves already carry the cumulative E. XY positions of thinned moves are not meaningful
(the simulator ignores them).

The whole file is read into memory (a 60 MB export needs a few hundred MB).
"""
import argparse
import os
import re
import sys

MOVE = re.compile(r'^G[0123](?=\s|$)', re.I)
E_WORD = re.compile(r'(?<![A-Za-z])E(-?\d*\.?\d+)', re.I)
Z_WORD = re.compile(r'(?<![A-Za-z])Z-?\d', re.I)
IMPORTANT = re.compile(
    r'^(T\d+\b|M10[49]\b|M14[01]\b|M190\b|M600\b|PAUSE\b|BMCU_|SET_PRINT_|SM_PRINT|'
    r'PRINT_START|PRINT_END|G28\b|CLEAR_PAUSE|RESUME\b)', re.I)
LAYER_MARK = re.compile(r'^;\s*(LAYER_CHANGE|Z:|HEIGHT:|layer num|CHANGE_LAYER|'
                        r'Change Tool|EXECUTABLE_BLOCK|HEADER_BLOCK|CONFIG_BLOCK)', re.I)
# Commands BMCU and Snapmaker never react to; --lean drops them (a long print has one or
# more per feature, which is most of what is left once the moves are thinned).
NOISE = re.compile(r'^(M73|M204|M205|M117|M486|SET_VELOCITY_LIMIT|'
                   r'EXCLUDE_OBJECT_START|EXCLUDE_OBJECT_END)\b', re.I)
HEADER_COMMENT = re.compile(r'^;\s*(total|filament used|estimated|max_layer_z|'
                            r'nozzle_temperature|filament_type)', re.I)


def _is_move(line):
    return bool(MOVE.match(line))


def _e_of(line):
    match = E_WORD.search(line)
    return float(match.group(1)) if match else None


def _set_e(line, value):
    return E_WORD.sub('E%.5f' % value, line, count=1)


def trim_lines(lines, every=12, edge=6, context=30, lean=False):
    """The trimmed list of lines (each without its newline)."""
    count = len(lines)
    keep = [False] * count
    in_config = False
    in_header = False
    first_layer = None
    for index, raw in enumerate(lines):
        line = raw.strip()
        if line.startswith(';') and 'HEADER_BLOCK_START' in line:
            in_header = True
        if line.startswith(';') and 'CONFIG_BLOCK_START' in line:
            in_config = True
        if first_layer is None and line.startswith(';') and 'LAYER_CHANGE' in line:
            first_layer = index
        if in_header or in_config:
            keep[index] = True
        if line.startswith(';') and 'HEADER_BLOCK_END' in line:
            in_header = False
        if line.startswith(';') and 'CONFIG_BLOCK_END' in line:
            in_config = False
        if not line:
            continue
        if line.startswith(';'):
            if LAYER_MARK.match(line) or HEADER_COMMENT.match(line):
                keep[index] = True
            continue
        if not _is_move(line):
            if lean and NOISE.match(line) and not (in_header or in_config):
                continue
            keep[index] = True                       # every command that is not a move
    end_start = count
    for index, raw in enumerate(lines):              # context around the important lines
        line = raw.strip()
        if line.startswith(';'):
            important = 'LAYER_CHANGE' in line or 'Change Tool' in line
        else:
            important = bool(IMPORTANT.match(line))
            if re.match(r'^(BMCU_)?PRINT_END', line, re.I) and first_layer is not None                     and index > first_layer:
                end_start = min(end_start, index)
        if important:
            for near in range(max(0, index - context), min(count, index + context + 1)):
                if lean and NOISE.match(lines[near].strip()):
                    continue
                keep[near] = True
    for index in range(count):                       # the start and the end G-code in full
        if (first_layer is not None and index < first_layer) or index >= end_start:
            if lean and NOISE.match(lines[index].strip()):
                continue
            keep[index] = True
    # thin the moves layer by layer, carrying the E of the dropped ones
    relative = True
    out = []
    carry = 0.0
    layer_moves = []                                  # indices of moves in this layer
    layer_start = 0

    def flush_layer(end):
        """Decide which moves of the finished layer to keep (called at a layer boundary)."""
        nonlocal carry
        total = len(layer_moves)
        chosen = set()
        for position, index in enumerate(layer_moves):
            if (keep[index] or position < edge or position >= total - edge or
                    position % every == 0 or Z_WORD.search(lines[index])):
                chosen.add(index)
        pending = 0.0
        for index in range(layer_start, end):
            raw = lines[index]
            line = raw.strip()
            if _is_move(line):
                e_value = _e_of(line)
                if index in chosen:
                    if relative and pending and e_value is not None:
                        raw = _set_e(raw, e_value + pending)
                        pending = 0.0
                    elif relative and pending and e_value is None:
                        out.append('G1 E%.5f F1800' % pending)
                        pending = 0.0
                    out.append(raw)
                elif relative and e_value is not None:
                    pending += e_value
                continue
            if keep[index]:
                out.append(raw)
        if relative and pending:
            out.append('G1 E%.5f F1800' % pending)
        layer_moves.clear()

    for index, raw in enumerate(lines):
        line = raw.strip()
        if line.upper().startswith('M83'):
            relative = True
        elif line.upper().startswith('M82'):
            relative = False
        if line.startswith(';') and 'LAYER_CHANGE' in line and index > layer_start:
            flush_layer(index)
            layer_start = index
        if _is_move(line):
            layer_moves.append(index)
    flush_layer(count)
    return out


def layer_extrusion(lines):
    """Net E (mm) per layer of a G-code file (index 0 is everything before the first layer)."""
    totals = [0.0]
    relative = True
    last = 0.0
    for raw in lines:
        line = raw.strip()
        upper = line.upper()
        if upper.startswith('M83'):
            relative = True
        elif upper.startswith('M82'):
            relative = False
        elif line.startswith(';') and 'LAYER_CHANGE' in line:
            totals.append(0.0)
        elif upper.startswith('G92'):
            match = E_WORD.search(line)
            if match:
                last = float(match.group(1))
        elif _is_move(line):
            e_value = _e_of(line)
            if e_value is None:
                continue
            if relative:
                totals[-1] += e_value
            else:
                totals[-1] += e_value - last
                last = e_value
    return totals


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('input')
    parser.add_argument('output')
    parser.add_argument('--every', type=int, default=12)
    parser.add_argument('--edge', type=int, default=6)
    parser.add_argument('--context', type=int, default=None,
                        help='lines kept around each tool change, temperature, BMCU '
                             'command and layer change (default 30, or 3 with --lean)')
    parser.add_argument('--lean', action='store_true',
                        help='for long prints: drop commands BMCU and Snapmaker never '
                             'react to (M73, M204, SET_VELOCITY_LIMIT, ...), keep less '
                             'context, and keep fewer moves per layer')
    args = parser.parse_args(argv)
    context = args.context if args.context is not None else (3 if args.lean else 30)
    edge = min(args.edge, 2) if args.lean else args.edge
    with open(args.input, encoding='utf-8', errors='replace', newline='') as stream:
        lines = [line.rstrip('\r\n') for line in stream]
    trimmed = trim_lines(lines, args.every, edge, context, lean=args.lean)
    with open(args.output, 'w', encoding='utf-8', newline='\n') as stream:
        stream.write('\n'.join(trimmed) + '\n')
    size = os.path.getsize(args.output)
    sys.stderr.write('%d -> %d lines, %.2f MB\n' % (len(lines), len(trimmed), size / 1e6))
    if size > 1.2e6:
        sys.stderr.write('Still over 1 MB: try --lean, then a larger --every.\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
