"""Provider-enforced message shapes; broker authorization remains separate."""


def _object(properties):
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


def _enum(*values):
    return {"type": "string", "enum": list(values)}


TEXT = {"type": "string"}
def _text(maximum, minimum=0):
    return {"type": "string", "minLength": minimum, "maxLength": maximum}


RESPONSE_SCHEMA = _object({"message": {"anyOf": [
    _object({"status": _enum("conversational"), "answer": TEXT}),
    _object({"status": _enum("declined"), "declined_reason": TEXT}),
    _object({"status": _enum("answered"), "answer": TEXT,
             "basis": _enum("current", "reference", "attachment"),
             "citations": {"type": "array", "items": TEXT}}),
    _object({"request": _enum("search", "sql"), "query": TEXT}),
    _object({"request": _enum("read"), "path": TEXT}),
    _object({"request": _enum("github_pulls"), "repository": TEXT,
             "state": _enum("all", "open", "closed"),
             "sort": _enum("created", "updated")}),
    _object({"request": _enum("github_pull"), "repository": TEXT,
             "number": {"type": "integer"}}),
    _object({"request": _enum("telegram_document"), "message_id": {"type": "integer"}}),
    _object({"request": _enum("tracker_status"), "proposal_id": TEXT}),
    _object({"request": _enum("tracker_propose"), "proposal": _object({
        "repository": _enum("leviathan-news/squid-bot"), "summary": _text(2000, 1),
        "items": {"type": "array", "minItems": 1, "maxItems": 30, "items": _object({
            "title": _text(200, 1), "description": _text(4000), "bead_id": _text(128),
            "github_number": {"type": "integer", "minimum": 0, "maximum": 10000000},
            "operation": _enum("create", "update", "review"),
            "priority": {"type": "integer", "minimum": 0, "maximum": 4}, "owner": _text(200), "due": _text(64),
            "evidence": _text(4000),
        })},
    })}),
]}})
