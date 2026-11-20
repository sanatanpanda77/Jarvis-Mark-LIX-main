"""
Plugin — Face Lock: the assistant works only for its owner's face.

    "Look at my camera and activate face recognition"
        → the window turns into a face scan: straight on, a little left, a
          little right. The face is kept as numbers, on this computer only.
    The next start
        → "FACE ACTIVATION REQUIRED". The owner looks at the camera, turns
          their head the way the arrows ask, and is greeted by the nickname
          they chose ("Welcome, Strongest Avenger"). Anyone else: "Access
          denied" - and the assistant does not listen to them.
    "Lock yourself" → the same lock, now.
    "Forget my face" → the face is deleted and nothing locks any more.

Switching the plugin on starts the face scan by itself.

HOW IT LOCKS
    Through core/inputlock, the lock any plugin can hold: while it is held this
    computer's microphone is not listened to and the window is covered (no
    typing, no settings). Requests from the phone still work - they come through
    the owner's own linked account. If the camera breaks or the face is not
    recognised, "Unlock another way" on the lock screen takes the licence key.
    Without a licence key (the open-source edition) it never locks at start.

WHAT IT IS NOT
    A lock against people at the keyboard - family, coworkers - not against a
    prepared attack: the head-turn check stops a photo, not a video made for it
    or a skilled mask, and a webcam is not an infrared face sensor. The
    computer's own sign-in stays the real lock.

THE CAMERA
    The one chosen in AUDIO & CAMERA (core/cameras, by name), watched while it
    is used: a camera that stops filming is replaced, and when another program
    holds it the lock screen names that program.

The seeing and drawing: _face_lock_core.py. The models: models/face (NOTICE).
"""
from __future__ import annotations

import threading
import time

from core.lazyimport import installed

if not installed("cv2"):        # pragma: no cover
    raise ImportError("face_lock needs OpenCV, which is not installed. "
                      "Run: pip install opencv-python")

try:
    from plugins import _face_lock_core as fc
except ImportError:             # loaded outside the package
    import _face_lock_core as fc  # type: ignore

from core import inputlock

_NS = "face_lock"
_OWNER = "face_lock"
_FPS = 15
_MATCH_FRAMES = 3            # frames in a row that must match
_STRANGER_FRAMES = 10        # frames in a row of somebody else before "Access denied"
_TURN_SECONDS = 7.0
_DENIED_SHOWN = 2.5
_DENIED_SAID_EVERY = 3.0     # each denial said, never two at once
_ENROL_SECONDS = 45.0
_ENROL_PLAN = (("front", 4), ("left", 2), ("right", 2))

# Everything the lock screen says. English; shown in the interface language
# (tools/i18n/translate.py --phrases ships these, core/i18n.phrase finds them).
_T_REQUIRED = "Face activation required"
_T_ENROL = "Face enrollment"
_T_CONFIRMED = "Identity confirmed"
_T_SAVED = "Face saved"
_L_LOOK = "Look at the camera"
_L_CLOSER = "Come a little closer to the camera"
_L_ONE = "Only one face, please"
_L_STRAIGHT = "Look straight at the camera"
_L_LEFT = "Turn your head to the left"
_L_RIGHT = "Turn your head to the right"
_L_ENROL_LEFT = "Now turn your head a little to the left"
_L_ENROL_RIGHT = "Now turn your head a little to the right"
_L_AGAIN = "Let's try that again"
_L_DENIED = "Access denied"
_L_NO_CAMERA = "The camera is not available"
_L_IN_USE = "{app} is using the camera - close it"
_L_WELCOME = "Welcome, {name}"
_L_WELCOME_BACK = "Welcome back"
_L_DONE = "Your face is kept only on this computer"

PLUGIN = {
    # For the plugin manager (core/plugin_loader): never sent to a model.
    "level": "basic",
    "label": "Face Lock",
    "summary": ("Torvis works only for your face: it scans you at start and greets you by "
                "the name you choose; anyone else gets \"Access denied\"."),
    "setup": [
        "Needs a webcam - choose it in AUDIO & CAMERA.",
        "Switch it on and look at the camera - Torvis learns your face (straight, left, right).",
        "Write the name it should greet you with in its settings.",
        "From then on Torvis does not listen to this computer until it sees you. "
        "The phone still works.",
        "Camera broken or not recognised? \"Unlock another way\" on the lock screen takes "
        "your licence key.",
        "Say: \"Lock yourself\" to lock it now, \"Forget my face\" to delete your face.",
    ],
    # Literal: read without importing (core/plugin_loader.peek). Every line
    # below appears here - tools/test_face_lock.py checks it.
    "phrases": [
        "Face activation required",
        "Face enrollment",
        "Identity confirmed",
        "Face saved",
        "Look at the camera",
        "Come a little closer to the camera",
        "Only one face, please",
        "Look straight at the camera",
        "Turn your head to the left",
        "Turn your head to the right",
        "Now turn your head a little to the left",
        "Now turn your head a little to the right",
        "Let's try that again",
        "Access denied",
        "The camera is not available",
        "{app} is using the camera - close it",
        "Welcome, {name}",
        "Welcome back",
        "Your face is kept only on this computer",
    ],
    "name": "face_lock",
    "brief": "Face Lock: learn the owner's face, lock the assistant until it sees them, forget the face.",
    "description": (
        "Face Lock - the assistant works only for its owner's face, seen through the WEBCAM. "
        "action='enroll': learn (or learn again) the user's face - 'activate face recognition', "
        "'look at my camera and remember my face', 'set up face lock'. It runs a short scan "
        "and returns when it is done. "
        "action='lock': lock the assistant now until the owner's face is seen - 'lock "
        "yourself', 'lock Torvis'. "
        "action='forget': delete the saved face - 'forget my face', 'turn face recognition off'. "
        "action='status': whether a face is saved and the lock is on. "
        "Never use screen_process or the camera tools for these."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "'enroll', 'lock', 'forget' or 'status'"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": _NS,
    "title": "🔒  FACE LOCK",
    "fields": [
        {"key": "nickname", "label": "The name it greets you with", "type": "text",
         "placeholder": "e.g. Strongest Avenger - empty: your name"},
        {"key": "lock_on_start", "label": "Lock until it sees my face, every start",
         "type": "toggle", "default": True},
        {"key": "liveness", "label": "Ask me to turn my head (a photo cannot)",
         "type": "toggle", "default": True},
        {"key": "strictness", "label": "How close the match must be", "type": "choice",
         "options": ["Normal", "Strict", "Relaxed"], "default": "Normal"},
    ],
    "action": {"label": "SCAN MY FACE", "run": lambda values: _enrol_from_settings()},
}

_busy = threading.Lock()


# ── settings ─────────────────────────────────────────────────────────────────

def _setting(key: str, default=None):
    try:
        from memory.config_manager import get_plugin_setting
        val = get_plugin_setting(_NS, key, default)
    except Exception:
        return default
    return default if val in (None, "") else val


def _toggle(key: str, default: bool) -> bool:
    val = _setting(key, default)
    return val if isinstance(val, bool) else default


def _threshold() -> float:
    return fc.STRICTNESS.get(str(_setting("strictness", "Normal")), fc.STRICTNESS["Normal"])


def _nickname() -> str:
    nick = " ".join(str(_setting("nickname", "") or "").split())[:60]
    if nick:
        return nick
    try:
        from memory.config_manager import get_user_name
        return " ".join(str(get_user_name() or "").split())[:60]
    except Exception:
        return ""


def _camera_index() -> int | None:
    """The camera chosen in the devices panel (by name, core/cameras), or None:
    the first camera that films."""
    try:
        from core import cameras
        return cameras.chosen()
    except Exception:
        return None


def _enabled() -> bool:
    try:
        from memory.config_manager import get_plugin_enabled
        return get_plugin_enabled(PLUGIN["name"], False)
    except Exception:
        return False


# ── speaking ─────────────────────────────────────────────────────────────────

def _say(text: str, wait: float = 30.0) -> None:
    """Said as it is (in the user's language), once the assistant can speak."""
    def go():
        ui = inputlock.interface(wait)
        if ui is None:
            return
        deadline = time.monotonic() + wait
        while not inputlock.can_speak() and time.monotonic() < deadline:
            time.sleep(0.3)
        say = getattr(ui, "request_announce", None)
        if inputlock.can_speak() and callable(say):
            try:
                say(text)
            except Exception as e:
                print(f"[FaceLock] could not speak: {e}")
    threading.Thread(target=go, daemon=True, name="face-lock-say").start()


def _no_camera(tone: str = "bad") -> None:
    """Why there is no picture - by name when another program holds the camera
    (closing it brings the picture back by itself)."""
    from core import cameras
    app = cameras.in_use_by()
    if app:
        inputlock.show(_OWNER, line=_L_IN_USE, tone=tone, values={"app": app})
    else:
        inputlock.show(_OWNER, line=_L_NO_CAMERA, tone=tone)


def _hold_frames(cam, eyes, tone: str, seconds: float, **draw) -> None:
    """Keep the camera view moving in one colour for a moment (a result)."""
    end = time.monotonic() + seconds
    t0 = time.monotonic()
    while time.monotonic() < end:
        frame = cam.read()
        if frame is None:
            break
        faces = eyes.faces(frame)
        fc.draw(frame, faces[0] if faces else None, tone, time.monotonic() - t0, **draw)
        inputlock.frame(_OWNER, fc.jpeg(frame))
        time.sleep(1.0 / _FPS)


# ── learning the face ────────────────────────────────────────────────────────

def enrol() -> tuple[bool, str]:
    """Scan the face (straight, left, right) and keep it. (ok, what happened)."""
    if not fc.models_present():
        return False, "The face models are missing from this installation; reinstalling fixes it."
    if not _busy.acquire(blocking=False):
        return False, "A face scan is already running."
    cam = fc.Camera(_camera_index())
    try:
        if not cam.open():
            from core import cameras
            app = cameras.in_use_by()
            return False, (f"{app} is using the camera; closing it frees the camera." if app else
                           "No camera shows a live picture - it may be in use by another "
                           "program, or there is none.")
        eyes = fc.Eyes()
        inputlock.hold(_OWNER, _T_ENROL, _L_STRAIGHT, fallback=False)
        total = sum(n for _p, n in _ENROL_PLAN)
        feats = []
        t0 = time.monotonic()
        deadline = t0 + _ENROL_SECONDS
        for phase, count in _ENROL_PLAN:
            line = {"front": _L_STRAIGHT, "left": _L_ENROL_LEFT, "right": _L_ENROL_RIGHT}[phase]
            inputlock.show(_OWNER, line=line, tone="idle" if phase == "front" else "busy")
            got, last = 0, 0.0
            while got < count:
                now = time.monotonic()
                if now > deadline:
                    return False, ("The face scan ran out of time - the face was not seen clearly "
                                   "enough. Good light and looking at the camera help.")
                frame = cam.read()
                if frame is None:                  # a frozen camera is being replaced
                    _no_camera("idle")
                    time.sleep(0.3)
                    continue
                faces = eyes.faces(frame)
                face = faces[0] if faces else None
                shown = line
                if len(faces) > 1 and faces[1].w > faces[0].w * 0.6:
                    shown = _L_ONE
                elif face is not None and not fc.big_enough(face, frame):
                    shown = _L_CLOSER
                elif face is not None and face.score >= 0.9 and now - last > 0.25:
                    yaw = face.yaw()
                    pose = (abs(yaw) < fc.FRONTAL if phase == "front"
                            else yaw <= -0.10 if phase == "left" else yaw >= 0.10)
                    if pose:
                        feats.append(eyes.feature(frame, face))
                        got += 1
                        last = now
                inputlock.show(_OWNER, line=shown)
                fc.draw(frame, face, "idle" if phase == "front" else "busy", now - t0,
                        side=None if phase == "front" else phase, ticks=(len(feats), total))
                inputlock.frame(_OWNER, fc.jpeg(frame))
                time.sleep(1.0 / _FPS)
        if not fc.same_person(feats):
            return False, ("Different faces were seen during the scan. It needs only the owner "
                           "in view - try again.")
        import numpy as np
        fc.save_profile(np.stack(feats))
        inputlock.show(_OWNER, title=_T_SAVED, line=_L_DONE, tone="ok")
        _hold_frames(cam, eyes, "ok", 1.4, ticks=(total, total))
        print(f"[FaceLock] face saved ({len(feats)} views)", flush=True)
        return True, _after_enrol_note(len(feats))
    except Exception as e:
        print(f"[FaceLock] face scan failed: {type(e).__name__}: {e}")
        return False, f"The face scan failed ({type(e).__name__})."
    finally:
        cam.release()
        inputlock.release(_OWNER)
        _busy.release()


def _after_enrol_note(views: int) -> str:
    if not _toggle("lock_on_start", True):
        return (f"The user's face is saved ({views} views, only on this computer). Locking at "
                "start is switched off in Face Lock's settings, so it locks only when asked "
                "('lock yourself').")
    if not inputlock.fallback_available():
        return (f"The user's face is saved ({views} views, only on this computer), but this "
                "edition has no licence key to unlock it another way, so it never locks at start "
                "- only when asked ('lock yourself').")
    return (f"The user's face is saved ({views} views, only on this computer). Face Lock is on: "
            "from the next start the assistant will not listen to this computer until it sees "
            "the user's face. The phone keeps working, and 'Unlock another way' on the lock "
            "screen takes the licence key. Tell the user this briefly in their language.")


def _enrol_from_settings() -> tuple[bool, str]:
    ok, note = enrol()
    return ok, ("Face saved. From the next start Torvis needs to see you." if ok else note)


# ── the lock ─────────────────────────────────────────────────────────────────

def lock(reason: str = "") -> bool:
    """Lock now and watch the camera until the owner is seen. False when it
    cannot (no face saved, a scan already running)."""
    profile = fc.load_profile()
    if profile is None or not fc.models_present():
        return False
    if not _busy.acquire(blocking=False):
        return False
    inputlock.hold(_OWNER, _T_REQUIRED, _L_LOOK, fallback=True)
    print(f"[FaceLock] locked{' (' + reason + ')' if reason else ''}", flush=True)
    threading.Thread(target=_guard, args=(profile,), daemon=True, name="face-lock").start()
    return True


def _guard(profile) -> None:
    cam = fc.Camera(_camera_index())
    said = 0.0
    try:
        _say(_T_REQUIRED + ".")
        eyes = fc.Eyes()
        threshold = _threshold()
        liveness = _toggle("liveness", True)
        state, side, side_until = "scan", None, 0.0
        streak = strangers = 0
        denied_until = 0.0
        t0 = time.monotonic()
        while inputlock.holds(_OWNER):
            now = time.monotonic()
            frame = cam.read()
            if frame is None:
                _no_camera()
                inputlock.frame(_OWNER, fc.jpeg(fc.draw(fc.blank(), None, "bad", now - t0)))
                time.sleep(1.0)
                continue
            faces = eyes.faces(frame)
            face = faces[0] if faces else None
            line, tone, progress = _L_LOOK, "idle", None

            if face is None or not fc.big_enough(face, frame):
                streak = strangers = 0
                if state == "turn":
                    state, line = "scan", _L_AGAIN
                elif face is not None:
                    line = _L_CLOSER
            else:
                sim = fc.similarity(eyes.feature(frame, face), profile)
                if state == "scan":
                    if sim >= threshold:
                        streak, strangers = streak + 1, 0
                    elif sim < fc.STRANGER_BELOW:
                        # counted again once the last "Access denied" has been shown
                        streak, strangers = 0, strangers + (now >= denied_until)
                    else:
                        streak = max(0, streak - 1)
                    progress = streak / _MATCH_FRAMES
                    if streak >= _MATCH_FRAMES:
                        if liveness:
                            state, side, side_until = "turn", fc.pick_side(), now + _TURN_SECONDS
                        else:
                            state = "ok"
                    elif strangers >= _STRANGER_FRAMES:
                        strangers = 0
                        denied_until = now + _DENIED_SHOWN
                        print("[FaceLock] a face that is not the owner's", flush=True)
                        if now - said > _DENIED_SAID_EVERY:
                            said = now
                            _say(_L_DENIED + ".", wait=5.0)
                elif state == "turn":
                    if sim < fc.TURN_KEEP:
                        state, streak, line = "scan", 0, _L_AGAIN
                    elif fc.turned(face, side):
                        print(f"[FaceLock] head turned {side} (yaw {face.yaw():+.2f})", flush=True)
                        state = "ok"
                    elif now > side_until:
                        state, streak, line = "scan", 0, _L_AGAIN

            if state == "turn":
                line, tone = (_L_LEFT if side == "left" else _L_RIGHT), "busy"
            if now < denied_until and state == "scan":
                line, tone = _L_DENIED, "bad"
            if state == "ok":
                break
            inputlock.show(_OWNER, line=line, tone=tone)
            fc.draw(frame, face, tone, now - t0, progress=progress,
                    side=side if state == "turn" else None)
            inputlock.frame(_OWNER, fc.jpeg(frame))
            time.sleep(1.0 / _FPS)

        if state == "ok" and inputlock.holds(_OWNER):
            name = _nickname()
            if name:
                inputlock.show(_OWNER, title=_T_CONFIRMED, line=_L_WELCOME, tone="ok",
                               values={"name": name})
            else:
                inputlock.show(_OWNER, title=_T_CONFIRMED, line=_L_WELCOME_BACK, tone="ok")
            print("[FaceLock] owner recognised", flush=True)
            _say(f"Welcome, {name}." if name else "Welcome back.")
            _hold_frames(cam, eyes, "ok", 1.6, progress=1.0)
    except Exception as e:
        # A broken scan must not leave a lock nobody can lift by face: the
        # licence key still works, and the screen says what happened.
        print(f"[FaceLock] the lock's scan failed: {type(e).__name__}: {e}")
        inputlock.show(_OWNER, line=_L_NO_CAMERA, tone="bad")
        while inputlock.holds(_OWNER):
            time.sleep(1.0)
    finally:
        cam.release()
        inputlock.release(_OWNER)
        _busy.release()


# ── the tool ─────────────────────────────────────────────────────────────────

def run(parameters: dict, player=None, session_memory=None) -> str:
    action = str((parameters or {}).get("action") or "").strip().lower()
    if action in ("enroll", "enrol", "activate", "setup", "scan"):
        ok, note = enrol()
        return note if ok else f"status=failed reason={note} Say so briefly in the user's language."
    if action == "lock":
        if not fc.has_profile():
            return ("status=failed reason=no face is saved yet. Offer to learn the user's face "
                    "first (Face Lock, action='enroll').")
        if not inputlock.fallback_available():
            return ("status=failed reason=this edition has no licence key to unlock with if the "
                    "face is not recognised, so it does not lock.")
        if not lock("asked"):
            return "status=failed reason=a face scan is already running."
        return "Locked: the assistant waits for the owner's face. Confirm in a few words."
    if action in ("forget", "delete", "off"):
        gone = fc.forget_profile()
        return ("The saved face is deleted; nothing locks any more." if gone
                else "No face was saved, so there was nothing to delete.")
    if action == "status":
        if not fc.has_profile():
            return "No face is saved. Face Lock locks nothing until the user's face is learnt."
        on = _toggle("lock_on_start", True) and inputlock.fallback_available()
        return ("A face is saved. " + ("It locks at every start until it sees the owner."
                                       if on else "It locks only when asked."))
    return "status=failed reason=unknown action; use enroll, lock, forget or status."


# ── at start, and when switched on ───────────────────────────────────────────

_on_at_start = _enabled()
_offered = False


def describe():
    """Asked each time a session is built while the plugin is ON. Switched on
    during this run with no face saved: the face scan starts by itself."""
    global _offered
    if not _on_at_start and not _offered and not fc.has_profile():
        _offered = True
        threading.Thread(target=_first_scan, daemon=True, name="face-lock-first").start()
    return None


def _first_scan() -> None:
    if inputlock.interface(60) is None:
        return
    deadline = time.monotonic() + 30
    while not inputlock.can_speak() and time.monotonic() < deadline:
        time.sleep(0.3)
    _say("Face Lock is on. Look at the camera - I'll learn your face.", wait=10.0)
    time.sleep(3.0)
    ok, note = enrol()
    _say("Your face is saved. While Face Lock is on, Torvis needs to see your face each time "
         "it starts, and won't listen until it does." if ok else note)


def _lock_at_start() -> None:
    # The interface comes up right after plugin discovery and before the
    # microphone opens (main.py) - and never in a test that imports this file.
    if inputlock.interface(120) is None:
        return
    if not (_enabled() and _toggle("lock_on_start", True) and fc.has_profile()):
        return
    if not inputlock.fallback_available():
        print("[FaceLock] not locking at start: no licence key to unlock with otherwise")
        return
    lock("start")


if _on_at_start:
    threading.Thread(target=_lock_at_start, daemon=True, name="face-lock-start").start()
