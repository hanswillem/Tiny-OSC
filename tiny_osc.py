bl_info = {
    "name": "Tiny OSC",
    "author": "Hans Willem Gijzel & Cursor A.I.",
    "version": (2, 0, 0),
    "blender": (4, 5, 3),
    "location": "View3D > Sidebar > TD Osc",
    "description": "Receive OSC and map message addresses to arbitrary Blender properties via absolute datapaths.",
    "category": "Animation",
}

from pickle import TRUE
import bpy
import socket
import struct
import threading

try:
    from typing import Dict
except Exception:
    Dict = dict

# --- Config ---
HOLD_LAST = True            # If no new OSC value is received during this update, keep using the previous value
DEBUG = False               # set True for console logs
APPLY_INTERVAL = 0.016      # seconds; ~60 Hz continuous application

# --- Module state ---
_rx_thread = None
_stop_flag = False
_lock = threading.Lock()
_last_value = None
_current_host = "localhost"
_current_port = 10000
_rx_values: Dict[str, float] = {}
_last_values: Dict[str, float] = {}
_sock = None
_last_keyed_frame: Dict[str, int] = {}

# --- Minimal OSC parsing with bundle support and f/i/d ---

def _pad4(n): return (4 - (n % 4)) % 4

def _parse_msg(buf: bytes):
    i0 = buf.find(b"\x00")
    if i0 < 0: raise ValueError("no addr nul")
    addr = buf[:i0].decode("utf-8", "ignore")
    p = i0 + 1 + _pad4(i0 + 1)

    i1rel = buf[p:].find(b"\x00")
    if i1rel < 0: raise ValueError("no typetags nul")
    i1 = p + i1rel
    tags = buf[p:i1].decode("utf-8", "ignore")
    p = i1 + 1 + _pad4((i1 - 0) + 1)
    if not tags.startswith(","):
        raise ValueError("bad typetags")

    args = []
    for t in tags[1:]:
        if t == "f":
            args.append(struct.unpack(">f", buf[p:p+4])[0]); p += 4
        elif t == "i":
            args.append(float(struct.unpack(">i", buf[p:p+4])[0])); p += 4
        elif t == "d":
            args.append(struct.unpack(">d", buf[p:p+8])[0]); p += 8
        else:
            raise ValueError(f"unsupported type {t!r}")
    return addr, args

def _parse_osc(buf: bytes):
    if buf.startswith(b"#bundle\x00"):
        p = 16  # "#bundle\0" (8) + timetag (8)
        while p + 4 <= len(buf):
            (sz,) = struct.unpack(">i", buf[p:p+4]); p += 4
            if sz <= 0 or p + sz > len(buf): break
            msg = buf[p:p+sz]; p += sz
            try:
                yield _parse_msg(msg)
            except Exception:
                continue
    else:
        yield _parse_msg(buf)

# --- Network listener thread ---

def _listener():
    global _last_value, _sock
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _sock = sock
        sock.bind((_current_host, _current_port))
        sock.settimeout(0.1)
        if DEBUG: print(f"[OSC] Listening {_current_host}:{_current_port}")
    except Exception as e:
        print(f"[OSC] Failed to bind {_current_host}:{_current_port} -> {e}")
        return

    try:
        while not _stop_flag:
            try:
                data, _ = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break

            for addr, args in _parse_osc(data):
                if args:
                    v = float(args[0])
                    with _lock:
                        _last_value = v
                        _rx_values[addr] = v
                        _last_values[addr] = v
                    if DEBUG: print(f"[OSC] {addr} {v}")
    finally:
        try: sock.close()
        except: pass
        _sock = None
        if DEBUG: print("[OSC] Socket closed")

# --- Continuous apply timer (always while running) ---

def _apply_timer():
    if _stop_flag:
        return None

    wm = bpy.context.window_manager if bpy.context is not None else None
    scn = bpy.context.scene if bpy.context is not None else None
    if wm is None or scn is None or not getattr(wm, "oscrec_running", False):
        # When stopped, clear last shown value so it's obvious nothing is being received
        try:
            if wm is not None:
                wm.oscrec_last_value_text = ""
        except Exception:
            pass
        return APPLY_INTERVAL

    # Apply incoming values to each configured mapping (absolute datapaths)
    mappings = getattr(scn, "oscrec_mappings", [])
    for item in mappings:
        # Skip disabled mappings
        try:
            if hasattr(item, "enabled") and not item.enabled:
                continue
        except Exception:
            pass
        addr = item.address
        # Normalize mapping address to start with '/'
        if addr and not addr.startswith('/'):
            addr = '/' + addr
        val = None
        with _lock:
            if addr in _rx_values:
                val = _rx_values.get(addr)
            elif HOLD_LAST and addr in _last_values:
                val = _last_values.get(addr)
        if val is None:
            continue
        try:
            _apply_mapping_value(item, float(val))
        except Exception as e:
            print(f"[OSC] Failed to set datapath '{item.datapath}': {e}")
            continue

        # Optional keyframe recording on each frame while playing
        if getattr(wm, "oscrec_record_keys", False):
            playing = getattr(bpy.context.screen, "is_animation_playing", False)
            if playing:
                frame = bpy.context.scene.frame_current
                key = f"{item.datapath}"
                if _last_keyed_frame.get(key) != frame:
                    try:
                        _insert_keyframe_for_absolute(item.datapath, frame)
                        _last_keyed_frame[key] = frame
                    except Exception as e:
                        print(f"[OSC] Keyframe failed for '{item.datapath}': {e}")

    # Update status text while running
    try:
        with _lock:
            lv = _last_value
        wm.oscrec_last_value_text = (f"{lv:.4f}" if lv is not None else "")
    except Exception:
        pass
    # Ping UI to redraw so status updates
    _redraw_editors()
    return APPLY_INTERVAL

def _split_expr_index(expr: str):
    """Split a trailing [index] from a full python-style expression.
    Returns (base_expr, index_or_None)."""
    if expr.endswith("]"):
        lb = expr.rfind("[")
        if lb != -1:
            idx_str = expr[lb+1:-1]
            if idx_str.isdigit():
                return expr[:lb], int(idx_str)
    return expr, None

def _find_last_attr_dot(expr: str) -> int:
    """Return index of the last attribute dot (.) not inside brackets or quotes; -1 if none."""
    depth = 0
    in_str = False
    str_ch = ''
    last_dot = -1
    i = 0
    while i < len(expr):
        ch = expr[i]
        if in_str:
            if ch == str_ch:
                in_str = False
            elif ch == "\\" and i + 1 < len(expr):
                i += 1  # skip escaped char
        else:
            if ch in ('"', "'"):
                in_str = True
                str_ch = ch
            elif ch == '[':
                depth += 1
            elif ch == ']':
                depth = max(0, depth - 1)
            elif ch == '.' and depth == 0:
                last_dot = i
        i += 1
    return last_dot

def _split_owner_and_attr(base_expr: str):
    """Split owner python expr and attribute name from a base expression without [index]."""
    dot = _find_last_attr_dot(base_expr)
    if dot == -1:
        # e.g. base_expr could be just an attribute on a top-level object without dot (unlikely)
        raise ValueError(f"Cannot determine owner for expression: {base_expr}")
    owner_expr = base_expr[:dot]
    attr = base_expr[dot+1:]
    return owner_expr, attr

def _eval_expr(expr: str):
    return eval(expr, {"__builtins__": {} , "bpy": bpy}, {})

def _resolve_owner_attr_idx(abs_expr: str):
    """From an absolute expression like bpy.data.objects["Cube"].rotation_euler[2]
    return (owner_object, attr_name, index_or_None)."""
    base_expr, idx = _split_expr_index(abs_expr)
    owner_expr, attr = _split_owner_and_attr(base_expr)
    owner = _eval_expr(owner_expr)
    return owner, attr, idx

def _set_absolute_datapath_value(abs_expr: str, value: float):
    owner, attr, idx = _resolve_owner_attr_idx(abs_expr)
    if idx is None:
        # scalar property
        setattr(owner, attr, value)
    else:
        vec = getattr(owner, attr)
        vec[idx] = value

def _coerce_for_target(owner, attr: str, idx, value_f: float):
    # Determine target RNA type when AUTO
    def infer_target_type():
        try:
            prop = owner.bl_rna.properties.get(attr)
            if prop is None:
                # Attribute may be on nested RNA or custom; fallback to current value type
                cur = getattr(owner, attr)
                if idx is None:
                    return type(cur)
                else:
                    return type(cur[idx])
            subtype = prop.type  # 'BOOLEAN', 'INT', 'FLOAT', 'ENUM', etc.
            return subtype
        except Exception:
            try:
                cur = getattr(owner, attr)
                return type(cur if idx is None else cur[idx])
            except Exception:
                return float

    target = infer_target_type()
    # Handle RNA prop type strings
    if isinstance(target, str):
        t = target
    else:
        t = target.__name__ if hasattr(target, '__name__') else str(target)
    t = t.upper()
    if 'BOOL' in t:
        return bool(value_f > 0.0)
    if 'INT' in t:
        return int(round(value_f))
    # ENUM and others: cast to int index when close to int; else pass float
    if 'ENUM' in t:
        return int(round(value_f))
    return float(value_f)

def _apply_mapping_value(item, value_f: float):
    owner, attr, idx = _resolve_owner_attr_idx(item.datapath)
    coerced = _coerce_for_target(owner, attr, idx, value_f)
    if idx is None:
        setattr(owner, attr, coerced)
    else:
        vec = getattr(owner, attr)
        vec[idx] = coerced

def _insert_keyframe_for_absolute(abs_expr: str, frame: int):
    owner, attr, idx = _resolve_owner_attr_idx(abs_expr)
    if idx is None:
        owner.keyframe_insert(data_path=attr, frame=frame)
    else:
        owner.keyframe_insert(data_path=attr, index=idx, frame=frame)
    try:
        wm = bpy.context.window_manager if bpy.context is not None else None
        if wm and getattr(wm, "oscrec_record_keys", False):
            _set_fcurve_mute_for_path(owner, attr, (idx if idx is not None else 0), True)
    except Exception:
        pass

def _get_fcurve(id_owner, data_path: str, array_index: int):
    ad = getattr(id_owner, "animation_data", None)
    if not ad or not ad.action:
        return None
    for fc in ad.action.fcurves:
        if fc.data_path == data_path and fc.array_index == array_index:
            return fc
    return None

def _set_fcurve_mute_for_path(id_owner, data_path: str, array_index: int, mute: bool):
    fc = _get_fcurve(id_owner, data_path, array_index)
    if fc is not None:
        fc.mute = mute

def _set_fcurve_mute_for_absolute(abs_expr: str, mute: bool):
    try:
        owner, attr, idx = _resolve_owner_attr_idx(abs_expr)
        if idx is None:
            # Try index 0 for scalar fcurves if any
            _set_fcurve_mute_for_path(owner, attr, 0, mute)
        else:
            _set_fcurve_mute_for_path(owner, attr, idx, mute)
    except Exception:
        pass

def _apply_mute_state_all(scn, mute: bool):
    try:
        for item in getattr(scn, "oscrec_mappings", []):
            _set_fcurve_mute_for_absolute(item.datapath, mute)
    except Exception:
        pass

def _redraw_editors():
    try:
        scr = bpy.context.screen
        if not scr:
            return
        for area in scr.areas:
            if area.type in {'GRAPH_EDITOR', 'DOPESHEET_EDITOR', 'VIEW_3D', 'TIMELINE'}:
                area.tag_redraw()
    except Exception:
        pass

def _set_playback_running(should_play: bool):
    try:
        scr = bpy.context.screen
        if not scr:
            return
        playing = scr.is_animation_playing
        if should_play and not playing:
            bpy.ops.screen.animation_play()
        elif not should_play and playing:
            bpy.ops.screen.animation_play()
    except Exception:
        pass

def _record_toggle_update(self, context):
    wm = context.window_manager
    scn = context.scene
    _apply_mute_state_all(scn, wm.oscrec_record_keys)
    _set_playback_running(wm.oscrec_record_keys)
    # If we just turned recording off, unmute to resume live evaluation
    if not wm.oscrec_record_keys:
        _apply_mute_state_all(scn, False)
    _redraw_editors()

# --- Toggle logic via a BoolProperty with update callback ---

def _start_system():
    global _rx_thread, _stop_flag, _current_host, _current_port
    _stop_flag = False
    # Read the desired host/port from UI properties if available
    try:
        scn = bpy.context.scene if bpy.context is not None else None
        if scn is not None:
            _current_host = getattr(scn, "oscrec_host", _current_host)
            _current_port = int(getattr(scn, "oscrec_port", _current_port))
        else:
            _current_host, _current_port = _current_host, _current_port
    except Exception:
        _current_host, _current_port = _current_host, _current_port
    if _rx_thread is None or not _rx_thread.is_alive():
        _rx_thread = threading.Thread(target=_listener, name="OSC_RX", daemon=True)
        _rx_thread.start()
    # start/update timer (idempotent; re-registering is fine)
    try:
        bpy.app.timers.unregister(_apply_timer)
    except Exception:
        pass
    bpy.app.timers.register(_apply_timer, first_interval=APPLY_INTERVAL, persistent=True)
    print(f"[OSC] Active on {_current_host}:{_current_port}")


def _stop_system():
    global _stop_flag, _rx_thread, _sock
    _stop_flag = True
    # Force-break the listener by closing the socket immediately
    try:
        if _sock is not None:
            _sock.close()
    except Exception:
        pass
    try:
        if _rx_thread is not None:
            _rx_thread.join(timeout=1.0)
    except Exception:
        pass
    _rx_thread = None
    try:
        bpy.app.timers.unregister(_apply_timer)
    except Exception:
        pass
    try:
        _last_keyed_frame.clear()
    except Exception:
        pass
    # Clear rx state so nothing holds last values
    try:
        with _lock:
            _rx_values.clear()
            _last_values.clear()
            globals()['_last_value'] = None
    except Exception:
        pass
    # Turn off recording toggle and unmute curves
    try:
        wm = bpy.context.window_manager if bpy.context is not None else None
        if wm is not None:
            wm.oscrec_last_value_text = ""
            wm.oscrec_record_keys = False
            _set_playback_running(False)
            scn = bpy.context.scene if bpy.context is not None else None
            if scn is not None:
                _apply_mute_state_all(scn, False)
        _redraw_editors()
    except Exception:
        pass
    print("[OSC] Stopped.")


def _toggle_update(self, context):
    running = context.window_manager.oscrec_running
    if running:
        _start_system()
    else:
        _stop_system()

def _host_port_update(self, context):
    """Restart listener if host/port change while running."""
    try:
        if context.window_manager.oscrec_running:
            _stop_system()
            _start_system()
    except Exception:
        pass

# --- UI Panel ---
class OSCREC_PT_panel(bpy.types.Panel):
    bl_label = "Tiny OSC"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Tiny OSC"

    def draw(self, context):
        layout = self.layout
        wm = context.window_manager
        scn = context.scene
        col = layout.column(align=True)
        # Network Address & Port
        #col.label(text="Network:")
        row_net = col.row(align=True)
        row_net.prop(scn, "oscrec_host", text="")
        row_net.prop(scn, "oscrec_port", text="")
        # Start/Stop toggle
        col.prop(wm, "oscrec_running", text="Listen", toggle=True)
        #col.separator()
        # Record toggle (only available when running)
        row = col.row(align=True)
        row.enabled = (wm.oscrec_running and len(scn.oscrec_mappings) > 0)
        row.prop(wm, "oscrec_record_keys", text="Bake Keyframes", toggle=True)
        col.separator()
        # Mapping list
        for i, item in enumerate(scn.oscrec_mappings):
            group = col.column(align=True)
            # Header row: fold toggle, link icon, name text field expands to fill, delete button at far right
            header = group.row(align=True)
            # Backward compatibility: initialize name if empty
            if not getattr(item, "name", ""):
                try:
                    item.name = f"Mapping {i+1}"
                except Exception:
                    pass
            # Foldout toggle
            header.prop(
                item,
                "expanded",
                text="",
                icon=('TRIA_DOWN' if getattr(item, 'expanded', True) else 'TRIA_RIGHT'),
                emboss=False,
            )
            header.label(icon='LINKED')
            header.prop(item, "name", text="")
            header.prop(item, "enabled", text="", icon=('HIDE_OFF' if item.enabled else 'HIDE_ON'), icon_only=True, emboss=False)
            op = header.operator("oscrec.mapping_remove", text="", icon='X', emboss=False)
            op.index = i
            # Fields underneath (only visible when expanded)
            if getattr(item, "expanded", True):
                group.separator()
                sub = group.column(align=True)
                sub.prop(item, "address", text="Address")
                sub.prop(item, "datapath", text="Datapath")
            # Add spacing after each mapping box for visual separation
            col.separator()
        # Add button under last mapping; spacing now consistent
        col.operator("oscrec.mapping_add", text="", icon='ADD')
        col.separator()
        try:
            txt = getattr(wm, "oscrec_last_value_text", "")
        except Exception:
            txt = ""
        # Fallback to in-memory last value if the UI property hasn't been updated yet
        if not txt:
            try:
                with _lock:
                    lv = _last_value
                txt = (f"{lv:.4f}" if lv is not None else "")
            except Exception:
                pass
        col.label(text=f"Received: {txt}", icon="INFO_LARGE")

# --- Registration ---
class OSCREC_PG_Mapping(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty(
        name="Name",
        description="Display name for this mapping.",
        default="",
    )
    expanded: bpy.props.BoolProperty(
        name="Expanded",
        description="Show mapping details",
        default=True,
    )
    enabled: bpy.props.BoolProperty(
        name="Enabled",
        description="Apply live OSC and record keyframes for this mapping",
        default=True,
    )
    address: bpy.props.StringProperty(
        name="Address",
        description="The address to listen to.",
        default="",
    )
    datapath: bpy.props.StringProperty(
        name="Datapath",
        description="The full datapath to map to.",
        default="",
    )
    # Cast mode removed; conversion is auto-inferred from target RNA/property.

class OSCREC_OT_mapping_add(bpy.types.Operator):
    bl_idname = "oscrec.mapping_add"
    bl_label = "Add Mapping"
    bl_options = {'INTERNAL', 'UNDO'}
    bl_description = "Add a new OSC mapping"

    def execute(self, context):
        scn = context.scene
        new_item = scn.oscrec_mappings.add()
        # Default name: "Mapping N" where N is 1-based index
        try:
            new_item.name = f"Mapping {len(scn.oscrec_mappings)}"
            new_item.expanded = True
        except Exception:
            pass
        scn.oscrec_mappings_index = len(scn.oscrec_mappings) - 1
        return {'FINISHED'}

class OSCREC_OT_mapping_remove(bpy.types.Operator):
    bl_idname = "oscrec.mapping_remove"
    bl_label = "Remove Mapping"
    bl_options = {'INTERNAL', 'UNDO'}

    index: bpy.props.IntProperty(default=-1)

    def execute(self, context):
        scn = context.scene
        idx = self.index if self.index >= 0 else scn.oscrec_mappings_index
        if 0 <= idx < len(scn.oscrec_mappings):
            scn.oscrec_mappings.remove(idx)
            scn.oscrec_mappings_index = min(idx, len(scn.oscrec_mappings) - 1)
        return {'FINISHED'}

classes = (
    OSCREC_PG_Mapping,
    OSCREC_OT_mapping_add,
    OSCREC_OT_mapping_remove,
    OSCREC_PT_panel,
)

def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.WindowManager.oscrec_running = bpy.props.BoolProperty(
        name="OSC Receiver Running",
        description="Start/Stop the OSC connection",
        default=False,
        update=_toggle_update,
    )
    bpy.types.Scene.oscrec_host = bpy.props.StringProperty(
        name="IP Address",
        description="The network IP address to bind (use 0.0.0.0 for all interfaces)",
        default=_current_host,
        update=_host_port_update,
    )
    bpy.types.Scene.oscrec_port = bpy.props.IntProperty(
        name="Port",
        description="UDP port to listen on",
        default=_current_port,
        min=1,
        max=65535,
        update=_host_port_update,
    )
    bpy.types.WindowManager.oscrec_record_keys = bpy.props.BoolProperty(
        name="Record Keyframes",
        description="Record keyframes for each mapping.",
        default=False,
        update=_record_toggle_update,
    )
    bpy.types.Scene.oscrec_mappings = bpy.props.CollectionProperty(type=OSCREC_PG_Mapping)
    bpy.types.Scene.oscrec_mappings_index = bpy.props.IntProperty(default=-1)
    bpy.types.WindowManager.oscrec_last_value_text = bpy.props.StringProperty(default="")


def unregister():
    # Stop system if running
    try:
        bpy.context.window_manager.oscrec_running = False
    except Exception:
        _stop_system()
    # Remove UI + properties
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
    if hasattr(bpy.types.WindowManager, "oscrec_running"):
        del bpy.types.WindowManager.oscrec_running
    if hasattr(bpy.types.Scene, "oscrec_host"):
        del bpy.types.Scene.oscrec_host
    if hasattr(bpy.types.Scene, "oscrec_port"):
        del bpy.types.Scene.oscrec_port
    if hasattr(bpy.types.WindowManager, "oscrec_record_keys"):
        del bpy.types.WindowManager.oscrec_record_keys
    if hasattr(bpy.types.Scene, "oscrec_mappings"):
        del bpy.types.Scene.oscrec_mappings
    if hasattr(bpy.types.Scene, "oscrec_mappings_index"):
        del bpy.types.Scene.oscrec_mappings_index
    if hasattr(bpy.types.WindowManager, "oscrec_last_value_text"):
        del bpy.types.WindowManager.oscrec_last_value_text

if __name__ == "__main__":
    register()
