"""recall_conversation — search what was said in earlier conversations
(memory/conversations.py), and switch that memory on or off or clear it."""
from __future__ import annotations


def _assistant_name() -> str:
    try:
        from memory.config_manager import load_api_keys
        return (load_api_keys().get("assistant_name") or "JARVIS").strip() or "JARVIS"
    except Exception:
        return "JARVIS"


def recall_conversation(parameters: dict, player=None) -> str:
    from memory import conversations as conv
    a = parameters or {}
    action = str(a.get("action") or "search").strip().lower()
    try:
        if action == "search":
            return conv.search(a.get("query") or "", a.get("date_from"), a.get("date_to"),
                               name=_assistant_name())
        if action == "status":
            s = conv.stats()
            state = "on" if s["enabled"] else "off"
            since = f", the oldest from {s['first']}" if s["first"] else ""
            return (f"Conversation memory is {state}. Kept: {s['days_with_conversations']} day(s) "
                    f"of conversations{since}, {s['summaries']} summaries; each day is kept for "
                    f"{s['days_kept']} days, on this computer only.")
        if action in ("on", "off"):
            from memory.config_manager import save_conversation_memory
            save_conversation_memory(enabled=(action == "on"))
            conv.refresh_settings()
            return ("Conversation memory is on: from now on what we say is kept on this "
                    "computer so I can recall it." if action == "on" else
                    "Conversation memory is off: nothing new will be kept. What was kept "
                    "before stays until you ask me to forget it.")
        if action == "keep_days":
            from memory.config_manager import save_conversation_memory
            days = int(a.get("days") or 0)
            if days < 1:
                return "Say how many days to keep conversations for."
            save_conversation_memory(days=days)
            conv.refresh_settings()
            gone = conv.prune(days)
            return f"Conversations are now kept for {days} days." + (
                f" {gone} older day(s) were deleted." if gone else "")
        if action == "forget":
            if not a.get("confirmed"):
                return ("Not deleted yet: ask the user to confirm exactly what to forget, then "
                        "call again with confirmed=true.")
            return conv.forget(a.get("date_from"), a.get("date_to"),
                               everything=bool(a.get("everything")))
        return f"Unknown action '{action}'."
    except Exception as e:
        return f"Could not reach the conversation memory: {type(e).__name__}: {e}"


TOOL = {
    "name": "recall_conversation",
    "description": (
        "Search what the user and you said in EARLIER conversations (kept on this computer). "
        "Call it whenever the user refers to something said before - 'what was that book I "
        "mentioned', 'what did we talk about yesterday', 'did I tell you about...', 'like I said "
        "last week' - and before saying you do not remember. Facts about the user are "
        "recall_memory; this is what was SAID. "
        "search: query = the key words (a name, a topic; leave empty to get the summaries of a "
        "time); date_from / date_to = YYYY-MM-DD, worked out from today's date for 'yesterday', "
        "'last week', 'in March'. Answer from the result in your own words; never read it out. "
        "status: is it on, how much is kept. on / off: switch keeping conversations. "
        "keep_days: how long to keep them (days). forget: delete kept conversations - "
        "date_from/date_to, or everything=true; ONLY after the user confirmed, with confirmed=true."),
    "brief": "Searches earlier conversations by words and dates; on/off, forget.",
    "tier": "core",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "enum": ["search", "status", "on", "off", "keep_days", "forget"]},
            "query": {"type": "STRING", "description": "for search: key words"},
            "date_from": {"type": "STRING", "description": "YYYY-MM-DD"},
            "date_to": {"type": "STRING", "description": "YYYY-MM-DD"},
            "days": {"type": "INTEGER", "description": "for keep_days"},
            "everything": {"type": "BOOLEAN", "description": "for forget: delete all"},
            "confirmed": {"type": "BOOLEAN", "description": "for forget: the user confirmed"},
        },
        "required": [],
    },
    "handler": recall_conversation,
}
