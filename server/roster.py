"""BUSY BEE roster: the agents, their rooms and how a task decides which room it belongs to.

This is the single place to edit when an agent joins, leaves or changes function.
`home_room` is where an idle/queued agent sits; `route_room` overrides it from the task title so
a working agent walks to the room that matches its work.
"""

# Rooms, in the order they appear on the floor plan (left to right), then the lounge.
ROOMS = [
    {"id": "coding", "name": "Coding", "accent": "#2f6fed"},
    {"id": "gamedev", "name": "Game Dev", "accent": "#8b5cf6"},
    {"id": "research", "name": "Research", "accent": "#22c55e"},
    {"id": "comms", "name": "Communications", "accent": "#22d3ee"},
    {"id": "art", "name": "Art", "accent": "#eab308"},
]
LOUNGE = {"id": "lounge", "name": "Lounge", "accent": "#f59e0b"}

# kanban assignees that are really the same person/role as another id.
ASSIGNEE_ALIAS = {
    "default": "orchestrator",
    "hermes": "orchestrator",
    "orchestrator": "orchestrator",
}

# The fleet. `mm` is the Mattermost user whose presence/chatter belongs to this agent.
AGENTS = [
    {"id": "coder", "name": "\U0001f535 coder", "role": "Backend & infra engineer",
     "home_room": "coding", "color": "#2f6fed", "mm": "coder"},
    {"id": "coder2", "name": "\U0001f7e3 coder2", "role": "Game & tools engineer",
     "home_room": "gamedev", "color": "#8b5cf6", "mm": "coder2"},
    {"id": "auditor", "name": "\U0001f7e0 auditor", "role": "Security & QA auditor",
     "home_room": "comms", "color": "#f59e0b", "mm": "auditor"},
    {"id": "maya", "name": "\U0001f7f5 maya", "role": "Communications & calendar",
     "home_room": "comms", "color": "#22d3ee", "mm": "maya"},
    {"id": "researcher", "name": "\U0001f7e2 researcher", "role": "Research & reporting",
     "home_room": "research", "color": "#22c55e", "mm": "researcher"},
    {"id": "orchestrator", "name": "\U0001f7e1 orchestrator", "role": "Hermes orchestrator (CEO)",
     "home_room": "art", "color": "#eab308", "mm": "hermes"},
]

# Task-title keywords that send a working agent to a different room than its home room.
# Strong words only: a false match sends an agent to the wrong room, a miss keeps it at home.
ROOM_KEYWORDS = {
    "gamedev": r"\b(game|gamedev|godot|unity|unreal|sprite|shader|level design|arcade|webgl|three\.js|pixel art)\b",
    "research": r"\b(research|researcher|benchmark|survey|literature|paper|study)\b",
    "comms": r"\b(mail|e-mail|email|calendar|meeting|mattermost|newsletter|announce|slack|blog|tweet|social)\b",
    "art": r"\b(logo|brand|poster|illustrat|icon|banner|thumbnail|slide|deck|art direction)\b",
    "coding": r"\b(api|backend|frontend|deploy(?:ment)?|docker|portainer|systemd|migration|refactor|database|postgres|redis|config|infrastructure|security|audit)\b",
}

DEFAULT_ROOM = "coding"


def canonical_assignee(raw):
    """Map a kanban assignee (default/hermes/orchestrator) onto the roster id."""
    key = (raw or "").strip().lower()
    return ASSIGNEE_ALIAS.get(key, key)


def agent_by_id(agent_id):
    for a in AGENTS:
        if a["id"] == agent_id:
            return a
    return None


def route_room(title, home_room):
    """Which room does a task of this title belong to? Falls back to the agent's home room."""
    import re
    text = (title or "").lower()
    for room_id, pattern in ROOM_KEYWORDS.items():
        if re.search(pattern, text):
            return room_id
    return home_room or DEFAULT_ROOM


def room_name(room_id):
    for r in ROOMS:
        if r["id"] == room_id:
            return r["name"]
    return LOUNGE["name"] if room_id == LOUNGE["id"] else room_id
