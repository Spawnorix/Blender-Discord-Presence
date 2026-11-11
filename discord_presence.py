bl_info = {
    "name": "Discord Rich Presence",
    "author": "Spawnorix",
    "version": (1, 0, 0),
    "blender": (2, 80, 0),
    "location": "Preferences > Add-ons > Discord Rich Presence",
    "description": "Shows 'Playing Blender' with your current file/mode as Discord Rich Presence",
    "category": "System",
}

import json
import os
import socket
import struct
import sys
import threading
import time
import uuid

import bpy

MIN_UPDATE_INTERVAL = 15.0
POLL_INTERVAL = 5.0
READ_TIMEOUT = 4.0
RETRY_INTERVAL = 3.0

OP_HANDSHAKE = 0
OP_FRAME = 1
OP_CLOSE = 2

MODE_NAMES = {
    "OBJECT": "Object Mode",
    "EDIT_MESH": "Editing a mesh",
    "EDIT_CURVE": "Editing a curve",
    "EDIT_SURFACE": "Editing a surface",
    "EDIT_TEXT": "Editing text",
    "EDIT_ARMATURE": "Editing an armature",
    "EDIT_METABALL": "Editing a metaball",
    "EDIT_LATTICE": "Editing a lattice",
    "POSE": "Posing",
    "SCULPT": "Sculpting",
    "PAINT_WEIGHT": "Weight painting",
    "PAINT_VERTEX": "Vertex painting",
    "PAINT_TEXTURE": "Texture painting",
    "PARTICLE": "Editing particles",
    "PAINT_GPENCIL": "Drawing (Grease Pencil)",
    "EDIT_GPENCIL": "Editing Grease Pencil",
    "SCULPT_GPENCIL": "Sculpting Grease Pencil",
    "PAINT_GREASE_PENCIL": "Drawing (Grease Pencil)",
    "EDIT_GREASE_PENCIL": "Editing Grease Pencil",
}

WORKSPACE_NAMES = {
    "Layout": "Layout",
    "Modeling": "Modeling",
    "Sculpting": "Sculpting",
    "UV Editing": "UV Editing",
    "Texture Paint": "Texture painting",
    "Shading": "Shading",
    "Animation": "Animating",
    "Rendering": "Rendering",
    "Compositing": "Compositing",
    "Geometry Nodes": "Geometry Nodes",
    "Scripting": "Scripting",
    "2D Animation": "2D Animating",
    "Video Editing": "Video editing",
}


class DiscordIPC:
    def __init__(self, client_id):
        self.client_id = client_id
        self._pipe = None
        self._sock = None

    def connect(self):
        if sys.platform == "win32":
            for i in range(10):
                try:
                    self._pipe = open(r"\\?\pipe\discord-ipc-%d" % i, "r+b", buffering=0)
                    break
                except OSError:
                    continue
        else:
            base = (os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("TMPDIR")
                    or os.environ.get("TMP") or os.environ.get("TEMP") or "/tmp")
            dirs = [base, os.path.join(base, "app/com.discordapp.Discord"),
                    os.path.join(base, "snap.discord")]
            for d in dirs:
                for i in range(10):
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    try:
                        s.settimeout(READ_TIMEOUT)
                        s.connect(os.path.join(d, "discord-ipc-%d" % i))
                        self._sock = s
                        break
                    except OSError:
                        s.close()
                if self._sock:
                    break
        if not self._pipe and not self._sock:
            raise ConnectionError("Discord is not running")

        self._send(OP_HANDSHAKE, {"v": 1, "client_id": self.client_id})
        op, data = self._recv()
        if op == OP_CLOSE or data.get("evt") != "READY":
            raise ConnectionError("Handshake rejected: %s" % data.get("message", data))

    def set_activity(self, activity):
        args = {"pid": os.getpid()}
        if activity:
            args["activity"] = activity
        self._send(OP_FRAME, {"cmd": "SET_ACTIVITY", "args": args, "nonce": str(uuid.uuid4())})
        op, data = self._recv()
        if op == OP_CLOSE:
            raise ConnectionError("Discord closed the connection")
        if data.get("evt") == "ERROR":
            raise ValueError(data.get("data", {}).get("message", "SET_ACTIVITY failed"))

    def close(self):
        try:
            if self._pipe or self._sock:
                self._send(OP_CLOSE, {})
        except OSError:
            pass
        for h in (self._pipe, self._sock):
            try:
                if h:
                    h.close()
            except OSError:
                pass
        self._pipe = self._sock = None

    def _send(self, op, payload):
        body = json.dumps(payload).encode("utf-8")
        frame = struct.pack("<II", op, len(body)) + body
        if self._pipe:
            self._pipe.write(frame)
            self._pipe.flush()
        else:
            self._sock.sendall(frame)

    def _wait_readable(self, timeout=READ_TIMEOUT):
        import ctypes
        import msvcrt
        handle = msvcrt.get_osfhandle(self._pipe.fileno())
        avail = ctypes.c_ulong(0)
        deadline = time.time() + timeout
        while True:
            if not ctypes.windll.kernel32.PeekNamedPipe(
                    ctypes.c_void_p(handle), None, 0, None, ctypes.byref(avail), None):
                raise ConnectionError("Discord pipe closed")
            if avail.value:
                return
            if time.time() > deadline:
                raise ConnectionError("Discord did not respond")
            time.sleep(0.05)

    def _read_exact(self, n):
        if self._pipe:
            self._wait_readable()
        buf = b""
        while len(buf) < n:
            chunk = self._pipe.read(n - len(buf)) if self._pipe else self._sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("Discord pipe closed")
            buf += chunk
        return buf

    def _recv(self):
        op, length = struct.unpack("<II", self._read_exact(8))
        return op, json.loads(self._read_exact(length).decode("utf-8"))


class PresenceWorker(threading.Thread):
    def __init__(self, client_id):
        super().__init__(daemon=True, name="DiscordPresence")
        self.client_id = client_id
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stopping = False
        self.pending = None
        self.sent = None
        self.status = "Connecting..."

    def push(self, activity):
        with self.lock:
            self.pending = activity
        self.wake.set()

    def stop(self):
        self.stopping = True
        self.wake.set()

    def run(self):
        ipc = None
        last_send = 0.0
        while not self.stopping:
            try:
                if ipc is None:
                    ipc = DiscordIPC(self.client_id)
                    ipc.connect()
                    self.sent = None
                    self.status = "Connected"

                with self.lock:
                    activity = self.pending
                interval = 1.0 if not activity or not self.sent else MIN_UPDATE_INTERVAL
                wait = interval - (time.time() - last_send)
                if activity is not None and activity != self.sent and wait <= 0:
                    ipc.set_activity(activity)
                    self.sent = activity
                    last_send = time.time()
                    self.status = "Connected"
                    continue

                self.wake.wait(max(wait, 1.0) if activity != self.sent else POLL_INTERVAL)
                self.wake.clear()
            except ValueError as e:
                self.status = "Error: %s" % e
                self.sent = activity
            except (OSError, ConnectionError, struct.error, json.JSONDecodeError) as e:
                self.status = "Waiting for Discord (%s)" % e
                if ipc:
                    ipc.close()
                ipc = None
                self.wake.wait(RETRY_INTERVAL)
                self.wake.clear()

        if ipc:
            try:
                ipc._send(OP_FRAME, {
                    "cmd": "SET_ACTIVITY",
                    "args": {"pid": os.getpid()},
                    "nonce": str(uuid.uuid4()),
                })
            except OSError:
                pass
            ipc.close()


_worker = None
_session_start = int(time.time())
_render_state = {"active": False, "frame": 0, "start": 0, "end": 0}


def _prefs():
    return bpy.context.preferences.addons[__name__].preferences


def _current_workspace():
    ws = getattr(bpy.context, "workspace", None)
    if ws is None:
        wm = bpy.context.window_manager
        if wm and wm.windows:
            ws = wm.windows[0].workspace
    return ws.name if ws else None


def _build_activity(p):
    if p.hide_filename:
        file_text = "Working on a project"
    elif bpy.data.filepath:
        name = bpy.path.basename(bpy.data.filepath)
        file_text = "Editing %s" % name + (" *" if bpy.data.is_dirty and p.show_unsaved else "")
    else:
        file_text = "Untitled project"

    if _render_state["active"]:
        if _render_state["end"] > _render_state["start"]:
            state = "Rendering frame %d / %d" % (_render_state["frame"], _render_state["end"])
        else:
            state = "Rendering"
    else:
        try:
            mode = bpy.context.mode
        except AttributeError:
            mode = "OBJECT"
        state = MODE_NAMES.get(mode, mode.replace("_", " ").title())
        workspace = _current_workspace() if p.show_workspace else None
        if workspace:
            ws_label = WORKSPACE_NAMES.get(workspace, workspace)
            if mode == "OBJECT" or state.lower() == ws_label.lower():
                state = ws_label
            else:
                state = "%s · %s" % (ws_label, state)
        if p.show_object_count:
            n = len(bpy.context.scene.objects) if bpy.context.scene else 0
            state += " · %d object%s" % (n, "" if n == 1 else "s")

    assets = {}
    if p.large_image:
        assets["large_image"] = p.large_image
        assets["large_text"] = "Blender %s" % bpy.app.version_string
    if p.show_engine and bpy.context.scene:
        engine = bpy.context.scene.render.engine
        assets["small_text"] = {"BLENDER_EEVEE": "EEVEE", "BLENDER_EEVEE_NEXT": "EEVEE",
                                "BLENDER_WORKBENCH": "Workbench", "CYCLES": "Cycles"}.get(engine, engine.title())
        if p.small_image:
            assets["small_image"] = p.small_image

    activity = {"details": file_text[:128], "state": state[:128], "assets": assets}
    if p.show_elapsed:
        activity["timestamps"] = {"start": _session_start}
    return activity


def _tick():
    try:
        p = _prefs()
    except KeyError:
        return None
    if _worker is not None:
        _worker.push(_build_activity(p) if p.enabled else {})
    return POLL_INTERVAL


def _start_worker():
    global _worker
    _stop_worker()
    p = _prefs()
    client_id = p.client_id.strip()
    if client_id:
        _worker = PresenceWorker(client_id)
        _worker.start()


def _stop_worker():
    global _worker
    if _worker is not None:
        _worker.stop()
        _worker = None


def _on_client_id_changed(self, context):
    if _worker is None or _worker.client_id != self.client_id.strip():
        _start_worker()
    _tick()


@bpy.app.handlers.persistent
def _render_init(scene, *_):
    _render_state.update(active=True, frame=scene.frame_current,
                         start=scene.frame_start, end=scene.frame_end)
    _tick()


@bpy.app.handlers.persistent
def _render_pre(scene, *_):
    _render_state["frame"] = scene.frame_current


@bpy.app.handlers.persistent
def _render_done(scene, *_):
    _render_state["active"] = False
    _tick()


class DiscordPresencePrefs(bpy.types.AddonPreferences):
    bl_idname = __name__

    enabled: bpy.props.BoolProperty(
        name="Show on Discord",
        description="Turn off to clear your Blender status from Discord",
        default=True,
        update=lambda self, context: _tick(),
    )
    client_id: bpy.props.StringProperty(
        name="Application ID",
        description="Your Discord application's ID; the application's name is shown as 'Playing <name>'",
        default="",
        update=_on_client_id_changed,
    )
    large_image: bpy.props.StringProperty(
        name="Large image",
        description="Art asset key uploaded to your Discord application, or an https:// image URL",
        default="blender",
    )
    small_image: bpy.props.StringProperty(
        name="Small image",
        description="Optional small badge (asset key or URL), shown with the render engine as tooltip",
        default="",
    )
    hide_filename: bpy.props.BoolProperty(name="Hide file name", default=False)
    show_unsaved: bpy.props.BoolProperty(name="Mark unsaved changes (*)", default=False)
    show_workspace: bpy.props.BoolProperty(name="Show workspace", default=True)
    show_object_count: bpy.props.BoolProperty(name="Show object count", default=False)
    show_engine: bpy.props.BoolProperty(name="Show render engine", default=True)
    show_elapsed: bpy.props.BoolProperty(name="Show elapsed time", default=True)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "enabled")
        layout.prop(self, "client_id")
        status = _status_text(self)
        layout.label(text="Status: " + status, icon="INFO")
        row = layout.row()
        row.prop(self, "large_image")
        row.prop(self, "small_image")
        col = layout.column(heading="Show")
        col.prop(self, "hide_filename")
        col.prop(self, "show_unsaved")
        col.prop(self, "show_workspace")
        col.prop(self, "show_object_count")
        col.prop(self, "show_engine")
        col.prop(self, "show_elapsed")
        layout.operator("wm.discord_presence_reconnect", icon="FILE_REFRESH")


def _status_text(p):
    if not p.enabled:
        return "Hidden"
    if _worker:
        return _worker.status
    return "Enter an Application ID to start"


class VIEW3D_PT_discord_presence(bpy.types.Panel):
    bl_label = "Discord Presence"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Discord"

    def draw(self, context):
        p = context.preferences.addons[__name__].preferences
        layout = self.layout
        layout.prop(p, "enabled", toggle=True,
                    icon="HIDE_OFF" if p.enabled else "HIDE_ON")
        layout.label(text=_status_text(p))
        layout.prop(p, "hide_filename")


class WM_OT_discord_presence_reconnect(bpy.types.Operator):
    bl_idname = "wm.discord_presence_reconnect"
    bl_label = "Reconnect to Discord"

    def execute(self, context):
        _start_worker()
        _tick()
        return {"FINISHED"}


_classes = (DiscordPresencePrefs, VIEW3D_PT_discord_presence, WM_OT_discord_presence_reconnect)
_handlers = (
    (bpy.app.handlers.render_init, _render_init),
    (bpy.app.handlers.render_pre, _render_pre),
    (bpy.app.handlers.render_complete, _render_done),
    (bpy.app.handlers.render_cancel, _render_done),
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    for handler_list, fn in _handlers:
        handler_list.append(fn)
    bpy.app.timers.register(_first_tick, first_interval=1.0, persistent=True)


def _first_tick():
    _start_worker()
    bpy.app.timers.register(_tick, persistent=True)
    return None


def unregister():
    for fn in (_tick, _first_tick):
        if bpy.app.timers.is_registered(fn):
            bpy.app.timers.unregister(fn)
    for handler_list, fn in _handlers:
        if fn in handler_list:
            handler_list.remove(fn)
    _stop_worker()
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
