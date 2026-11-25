"""Synthesises a small sliced print from the real Snapmaker U1 BMCU profile.

The start and end G-code come from ``machine_start_gcode`` / ``machine_end_gcode`` of
``printers/Snapmaker_U1/Snapmaker_Orca/machine/Snapmaker U1 BMCU.json``, rendered with a
mini evaluator for the Orca placeholders that file uses. ``change_filament_gcode`` is an Orca
script block; it is translated by hand in ``tool_change_gcode`` (``# MODEL:``) and
``test_sim_print.ProfileDriftTest`` checks the profile still contains the lines it emits.
"""
import json
import os
import re

import _support

PROFILE = os.path.join(
    _support.ROOT, 'printers', 'Snapmaker_U1', 'Snapmaker_Orca', 'machine',
    'Snapmaker U1 BMCU.json')


def load_profile():
    with open(PROFILE, encoding='utf-8') as stream:
        return json.load(stream)


class _Eval(dict):
    def __missing__(self, key):
        raise KeyError(key)


def _expr(text, ctx):
    text = text.strip()
    text = re.sub(r'\bis_extruder_used\[(\d+)\]', r'is_extruder_used[\1]', text)
    return eval(text, {'__builtins__': {}}, _Eval(ctx))


def render(template, ctx):
    """Orca placeholders: ``{if X}``/``{elsif X}``/``{else}``/``{endif}`` lines and
    inline ``{expression}``."""
    out = []
    stack = []          # (taken_any, active)
    active = True
    for line in template.split('\n'):
        stripped = line.strip()
        one = re.match(r'^\{if\s+([^{}]*)\}(.*)\{endif\}$', stripped)
        if one:
            if active and bool(_expr(one.group(1), ctx)):
                out.append(re.sub(r'\{([^{}]*)\}',
                                  lambda mm: _fmt(_expr(mm.group(1), ctx)), one.group(2)))
            continue
        m = re.match(r'^\{(if|elsif)\s+(.*)\}$', stripped)
        if m:
            kind, cond = m.groups()
            if kind == 'if':
                value = bool(_expr(cond, ctx)) if active else False
                stack.append((active, value, value))
                active = active and value
            else:
                parent, taken, _ = stack[-1]
                value = (not taken) and bool(_expr(cond, ctx)) if parent else False
                stack[-1] = (parent, taken or value, value)
                active = parent and value
            continue
        if stripped == '{else}':
            parent, taken, _ = stack[-1]
            stack[-1] = (parent, True, (not taken))
            active = parent and (not taken)
            continue
        if stripped == '{endif}':
            parent, _, _ = stack.pop()
            active = parent
            continue
        if not active:
            continue
        line = re.sub(r'\{([^{}]*)\}', lambda mm: _fmt(_expr(mm.group(1), ctx)), line)
        out.append(line)
    return '\n'.join(out)


def _fmt(value):
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value)


def start_gcode(tools, initial, materials, colors, temps, bed='Textured PEI Plate'):
    profile = load_profile()
    used = [i in tools for i in range(8)]
    ctx = {
        'is_extruder_used': used,
        'filament_type': [materials.get(i, 'PLA') for i in range(8)],
        'filament_colour': ['#%s' % colors.get(i, 'FFFFFF') for i in range(8)],
        'initial_extruder': initial,
        'nozzle_temperature': [temps.get(i, 220) for i in range(8)],
        'nozzle_temperature_initial_layer': [temps.get(i, 220) for i in range(8)],
        'bed_temperature_initial_layer_single': 60,
        'curr_bed_type': bed,
        'total_layer_count': 3,
    }
    return render(profile['machine_start_gcode'], ctx)


def end_gcode():
    profile = load_profile()
    return render(profile['machine_end_gcode'], {'print_sequence': 'by layer'})


def tool_change_gcode(previous, nxt, initial, layer, temps, first_layer_temps,
                      materials):
    """# MODEL: hand-translation of the profile's ``change_filament_gcode`` script
    (move_z 1.5, travel speed 350, preheat the next extruder, BMCU_TOOL_CHANGE, and
    BMCU_PRINT_PREEXTRUDE when the tool really changes)."""
    lines = ['; Change Tool%d -> Tool%d (layer %d)' % (previous, nxt, layer),
             'G91', 'G1 Z1.5 F600', 'G90', 'G1 F21000']
    if previous < 4:
        lines.append('MOVE_TO_XY_IDLE_POSITION_EXTRUDER')
    lines.append('M400')
    if not (layer < 0 and nxt == initial):
        lines.append('; nonblocking preheat for Tool%d' % nxt)
        temp = first_layer_temps[nxt] if layer == 0 else temps[nxt]
        lines.append('M104 S%d T%d' % (temp, nxt))
    lines.append('BMCU_TOOL_CHANGE TOOL=%d' % nxt)
    if previous != nxt and initial != nxt:
        lines.append('BMCU_PRINT_PREEXTRUDE TOOL=%d' % nxt)
    lines.append('G90')
    return '\n'.join(lines)


def build(plan, materials=None, colors=None, temps=None, extrude_per_segment=5,
          tower_mm=48.0):
    """``plan`` is a list of layers, each a list of tools printed in that layer, e.g.
    ``[[0], [4, 6], [3]]``. Returns the G-code text."""
    materials = dict(materials or {})
    colors = dict(colors or {})
    temps = dict(temps or {})
    tools = sorted(set(t for layer in plan for t in layer))
    initial = plan[0][0]
    temps = {t: temps.get(t, 220) for t in range(8)}
    first = dict((t, temps[t] + 5) for t in temps)
    out = ['; nozzle_temperature = %s' % ','.join(str(temps[t]) for t in range(8)),
           '; nozzle_temperature_initial_layer = %s' %
           ','.join(str(first[t]) for t in range(8)),
           start_gcode(tools, initial, materials, colors, temps)]
    previous = initial
    first_segment = True
    for layer_index, layer_tools in enumerate(plan):
        out.append(';LAYER_CHANGE\n;Z:%.2f\nG92 E0\nTIMELAPSE_TAKE_FRAME' % (0.2 * (layer_index + 1)))
        out.append(';AFTER_LAYER_CHANGE\nSET_PRINT_STATS_INFO TOTAL_LAYER=%d CURRENT_LAYER=%d'
                   % (len(plan), layer_index + 1))
        for tool in layer_tools:
            if first_segment:
                first_segment = False
            else:
                out.append(tool_change_gcode(previous, tool, initial, layer_index,
                                             temps, first, materials))
                # wipe tower: purge the new filament so the next layer prints wet
                out.append('M83\n' + '\n'.join(
                    'G1 X%d Y%d E%.2f F1200' % (150 + i * 3, 250, tower_mm / 6.0)
                    for i in range(6)))
            previous = tool
            out.append('M83')
            out.extend('G1 X%d Y%d E%.2f F1800' % (100 + i * 2, 100 + layer_index,
                                                     extrude_per_segment / 5.0)
                       for i in range(5))
    out.append(end_gcode())
    return '\n'.join(out) + '\n'
