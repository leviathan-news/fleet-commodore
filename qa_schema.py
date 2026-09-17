"""Provider-enforced message shapes; broker authorization remains separate."""


def _object(properties):
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


def _enum(*values):
    return {"type": "string", "enum": list(values)}


TEXT = {"type": "string"}
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
]}})
